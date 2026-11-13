"""Console REST API under ``/api/v1`` (PROTOCOL.md section B) and the node backplane route.

Every route is typed with the models of ``schemas.py``, so ``/openapi.json`` documents
the exact shapes the console codes against. The routers are:

- ``public_router``: ``GET /api/v1/meta``, readable without the API token so that a
  console can show the marking banner and ask for the token.
- ``api_router``: everything else under ``/api/v1``; each request needs
  ``Authorization: Bearer <token>`` when ``Settings.api_token`` is set.
- ``backplane_router``: ``GET`` on the simulation backplane path the runtime advertises
  in ``welcome`` (``runtime.SIM_BACKPLANE_PATH``), authenticated with a *node* token.

Errors are JSON: ``{detail}`` (``schemas.ErrorResponse``), ``{detail, current_version}``
for a version conflict (409, ``schemas.ConflictResponse``) and ``{detail, errors,
warnings}`` for anything that does not validate (422, ``schemas.FieldErrorResponse``).
``install_error_handlers(app)`` maps the runtime's exceptions to those; no stack trace
ever reaches a client.

CPU-heavy work (lines of position, accuracy maps, link budgets, look angles, footprints,
configuration validation) runs on the runtime's worker pool (three threads), never on
the event loop. The pure helpers (``compute_look_angles``, ``compute_link_budget``,
``compute_subpoints``, ``compute_footprints``, ``accuracy_bands``) are also used by the
command line (``petal-locate check`` and ``accuracy``).
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

import numpy as np
from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from petal_locate.config import SystemConfig, config_json_schema, dump_config
from petal_locate.config.models import EmitterModel, Region
from petal_locate.core.antennas import electrically_small, gain_dbi
from petal_locate.core.geodesy import angle_between_deg, lla_to_ecef, look_angles, wrap_lon_deg
from petal_locate.core.geometry import footprint
from petal_locate.core.linkbudget import path_budget
from petal_locate.core.orbits import orbit_position, subsatellite_point
from petal_locate.core.timebase import ns_from_iso, sec
from petal_locate.server.api import schemas
from petal_locate.server.api.deps import bearer_token, get_runtime, require_api_token
from petal_locate.server.pipeline.events import EventNotFound, EventRecord, MeasurementEntry, event_truth
from petal_locate.server.runtime import (
    SIM_BACKPLANE_PATH,
    ConfigInvalid,
    NotSimulating,
    PreconditionRequired,
    PresetNotFound,
    ScenarioInvalid,
    ServerRuntime,
    StoreError,
    VersionConflict,
    resolve_truth_emitters,
)

__all__ = [
    "ACCURACY_BAND_CELLS",
    "ACCURACY_MAX_CELLS",
    "ACCURACY_MAX_WAITING",
    "ACCURACY_TIMEOUT_S",
    "LOP_MAX_CELLS",
    "MAX_BODY_BYTES",
    "ApiProblem",
    "ApiState",
    "CatchAllMiddleware",
    "accuracy_bands",
    "accuracy_config",
    "accuracy_problems",
    "api_router",
    "backplane_router",
    "compute_accuracy",
    "compute_footprints",
    "compute_link_budget",
    "compute_look_angles",
    "compute_subpoints",
    "install_error_handlers",
    "lop_step_deg",
    "public_router",
    "resolve_time_ns",
    "sim_truth",
]

logger = logging.getLogger(__name__)

API_PREFIX: Final[str] = schemas.API_PREFIX

MAX_BODY_BYTES: int = 4 * 1024 * 1024
"""Largest request body accepted (a configuration or scenario document), bytes.
The limits below are module variables so that a deployment (or a test) can tune them."""

ACCURACY_MAX_CELLS: int = 20_000
"""Largest accuracy grid computed (cells); a finer request is refused with 422."""

ACCURACY_TIMEOUT_S: float = 120.0
"""Time limit of one accuracy map, s (504 beyond it; checked between bands)."""

ACCURACY_BAND_CELLS: int = 256
"""Cells per worker job of an accuracy map: the grid is computed in latitude bands so
that the pipeline's own jobs interleave and the time limit can stop the work."""

ACCURACY_MAX_WAITING: int = 4
"""Accuracy requests allowed to wait while another map is computed (503 beyond)."""

ACCURACY_CACHE_SIZE: Final[int] = 8
"""Accuracy maps kept for repeated identical requests."""

LOP_MAX_CELLS: int = 250_000
"""Largest grid used to contour one line of position (cells)."""

LOP_MIN_STEP_DEG: Final[float] = 0.25
"""Finest automatic grid step of lines of position, degrees."""

LOP_K_SIGMA: Final[float] = 2.0
"""Half-width of the band around each line of position, standard deviations."""

LOP_CACHE_SIZE: Final[int] = 64
"""Line-of-position collections kept (per event solution and measurement set)."""

_JSON_TYPES: Final[frozenset[str]] = frozenset({"application/json", "text/json"})
_YAML_TYPES: Final[frozenset[str]] = frozenset(
    {"application/yaml", "application/x-yaml", "text/yaml", "text/x-yaml"}
)
_SNIFF_TYPES: Final[frozenset[str]] = frozenset({"", "text/plain", "application/octet-stream"})
_CONFIG_ENVELOPE_KEYS: Final[frozenset[str]] = frozenset({"version", "config", "warnings", "derived_monitors"})
_SCENARIO_ENVELOPE_KEYS: Final[frozenset[str]] = frozenset({"version", "scenario"})

_SUMMARIES = TypeAdapter(list[schemas.EventSummary])
_PRESETS = TypeAdapter(list[schemas.Preset])
_SUBPOINTS = TypeAdapter(list[schemas.Subpoint])
_FOOTPRINTS = TypeAdapter(list[schemas.Footprint])
_LOOK_ANGLES = TypeAdapter(list[schemas.LookAngle])


# --------------------------------------------------------------------------- errors


class ApiProblem(Exception):
    """An error reply decided by a route: HTTP ``status_code`` with ``{detail}``, or
    ``{detail, errors, warnings}`` when ``errors`` is given (422)."""

    def __init__(
        self,
        status_code: int,
        detail: str,
        *,
        errors: Sequence[Mapping[str, str]] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status_code = int(status_code)
        self.detail = detail
        self.errors = None if errors is None else [{"loc": str(e["loc"]), "msg": str(e["msg"])} for e in errors]
        self.headers = dict(headers or {})


def _error(status_code: int, detail: str, headers: Mapping[str, str] | None = None) -> JSONResponse:
    body = schemas.ErrorResponse(detail=detail)
    return JSONResponse(body.model_dump(mode="json"), status_code=status_code, headers=dict(headers or {}))


def _field_errors(
    detail: str,
    errors: Sequence[Mapping[str, str]],
    warnings: Sequence[Mapping[str, str]] = (),
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    body = schemas.FieldErrorResponse(
        detail=detail,
        errors=[schemas.FieldProblem(loc=str(e["loc"]), msg=str(e["msg"])) for e in errors],
        warnings=[schemas.FieldProblem(loc=str(w["loc"]), msg=str(w["msg"])) for w in warnings],
    )
    return JSONResponse(body.model_dump(mode="json"), status_code=422, headers=dict(headers or {}))


def _summary(errors: Sequence[Mapping[str, str]], what: str) -> str:
    if not errors:
        return f"the {what} does not validate"
    first = errors[0]
    more = f" (and {len(errors) - 1} more)" if len(errors) > 1 else ""
    where = f"{first['loc']}: " if first["loc"] not in ("", "(root)") else ""
    return f"the {what} does not validate: {where}{first['msg']}{more}"


def install_error_handlers(app: FastAPI) -> None:
    """Map the runtime's exceptions and request-validation failures to JSON replies.

    404: ``EventNotFound``, ``NotSimulating``, ``PresetNotFound``. 409:
    ``VersionConflict`` (``{detail, current_version}`` and the current ETag). 422:
    ``ConfigInvalid``, ``ScenarioInvalid``, request validation (``{detail, errors}``;
    ``loc`` without FastAPI's ``body`` / ``query`` prefix). 428: ``PreconditionRequired``.
    500: ``StoreError`` and anything unexpected (logged with its traceback; the client
    gets one sentence)."""

    @app.exception_handler(ApiProblem)
    async def _api_problem(request: Request, exc: ApiProblem) -> JSONResponse:
        if exc.errors is not None:
            return _field_errors(exc.detail, exc.errors, headers=exc.headers)
        return _error(exc.status_code, exc.detail, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _request_invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors: list[dict[str, str]] = []
        for error in exc.errors():
            loc = [str(p) for p in error.get("loc", ())]
            if loc and loc[0] in ("body", "query", "path", "header"):
                loc = loc[1:]
            errors.append({"loc": ".".join(loc) or "(root)", "msg": str(error.get("msg", "invalid"))})
        return _field_errors(_summary(errors, "request"), errors)

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(request: Request, exc: StarletteHTTPException) -> Response:
        if exc.status_code in (204, 304):
            return Response(status_code=exc.status_code, headers=getattr(exc, "headers", None))
        detail = exc.detail if isinstance(exc.detail, str) else json.dumps(exc.detail, default=str)
        return _error(exc.status_code, detail, getattr(exc, "headers", None))

    @app.exception_handler(EventNotFound)
    async def _event_not_found(request: Request, exc: EventNotFound) -> JSONResponse:
        return _error(404, str(exc))

    @app.exception_handler(NotSimulating)
    async def _not_simulating(request: Request, exc: NotSimulating) -> JSONResponse:
        return _error(404, str(exc))

    @app.exception_handler(PresetNotFound)
    async def _preset_not_found(request: Request, exc: PresetNotFound) -> JSONResponse:
        return _error(404, str(exc))

    @app.exception_handler(VersionConflict)
    async def _conflict(request: Request, exc: VersionConflict) -> JSONResponse:
        body = schemas.ConflictResponse(detail=str(exc), current_version=exc.current_version)
        headers = {"ETag": _etag(exc.current_version)} if exc.current_version >= 0 else {}
        return JSONResponse(body.model_dump(mode="json"), status_code=409, headers=headers)

    @app.exception_handler(PreconditionRequired)
    async def _precondition(request: Request, exc: PreconditionRequired) -> JSONResponse:
        return _error(428, str(exc))

    @app.exception_handler(ConfigInvalid)
    async def _config_invalid(request: Request, exc: ConfigInvalid) -> JSONResponse:
        return _field_errors(_summary(exc.errors, "configuration"), exc.errors)

    @app.exception_handler(ScenarioInvalid)
    async def _scenario_invalid(request: Request, exc: ScenarioInvalid) -> JSONResponse:
        return _field_errors(_summary(exc.errors, "scenario"), exc.errors)

    @app.exception_handler(StoreError)
    async def _store_error(request: Request, exc: StoreError) -> JSONResponse:
        logger.error("storage failure on %s %s: %s", request.method, request.url.path, exc,
                     extra={"path": request.url.path})
        return _error(500, "the server could not store the change (storage failure); see the server log")

    app.add_middleware(CatchAllMiddleware)


class CatchAllMiddleware:
    """Answers HTTP 500 ``{detail}`` for any exception a route lets escape, logged once
    with its traceback here. (Starlette's own catch-all re-raises after answering, so
    the ASGI server would log every failure a second time.) WebSocket handlers catch
    their own failures."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def tracking_send(message: Mapping[str, Any]) -> None:
            nonlocal started
            if message.get("type") == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception:
            logger.exception("unhandled error on %s %s", scope.get("method"), scope.get("path"),
                             extra={"path": scope.get("path")})
            if started:
                raise  # the reply is under way: the server can only drop the connection
            await _error(500, "internal error; see the server log")(scope, receive, send)


_ERRORS_404: Final[dict[int | str, dict[str, Any]]] = {404: {"model": schemas.ErrorResponse}}
_ERRORS_422: Final[dict[int | str, dict[str, Any]]] = {422: {"model": schemas.FieldErrorResponse}}
_AUTH: Final[dict[int | str, dict[str, Any]]] = {
    401: {"model": schemas.ErrorResponse, "description": "API token missing or wrong"},
    422: {"model": schemas.FieldErrorResponse, "description": "the request does not validate"},
    500: {"model": schemas.ErrorResponse, "description": "internal error (details in the server log)"},
    503: {"model": schemas.ErrorResponse, "description": "the server is starting"},
}


# --------------------------------------------------------------------------- per-app state


@dataclass
class ApiState:
    """Caches and gates of the API, one per application (``app.state.api``)."""

    lop_cache: collections.OrderedDict[tuple[Any, ...], bytes] = field(default_factory=collections.OrderedDict)
    accuracy_cache: collections.OrderedDict[tuple[Any, ...], bytes] = field(default_factory=collections.OrderedDict)
    accuracy_gate: asyncio.Semaphore | None = None
    accuracy_waiting: int = 0
    backplane: tuple[int, str, bytes] | None = None
    config_schema: bytes | None = None

    def gate(self) -> asyncio.Semaphore:
        """The one-at-a-time gate of accuracy maps (created on the serving loop)."""
        if self.accuracy_gate is None:
            self.accuracy_gate = asyncio.Semaphore(1)
        return self.accuracy_gate


def _api_state(request: Request) -> ApiState:
    state: ApiState | None = getattr(request.app.state, "api", None)
    if state is None:
        state = ApiState()
        request.app.state.api = state
    return state


def _remember(cache: collections.OrderedDict[tuple[Any, ...], bytes], key: tuple[Any, ...], value: bytes,
              limit: int) -> None:
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > limit:
        cache.popitem(last=False)


# --------------------------------------------------------------------------- small helpers


def _etag(version: int) -> str:
    return f'"{int(version)}"'


def _json(model: Any, *, status_code: int = 200, headers: Mapping[str, str] | None = None) -> Response:
    """A JSON reply serialised once by pydantic (``*_ns`` as strings, never NaN)."""
    return Response(model.model_dump_json(), status_code=status_code, media_type="application/json",
                    headers=dict(headers or {}))


def _json_list(adapter: TypeAdapter[Any], items: Any) -> Response:
    return Response(adapter.dump_json(items), media_type="application/json")


def _finite(value: Any) -> float | None:
    """A finite float, or None (NaN, infinities, None)."""
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _now_ns() -> int:
    return time.time_ns()


def resolve_time_ns(t: str | None, *, now_ns: int | None = None, loc: str = "t") -> int:
    """``t`` (ISO 8601 UTC) as integer ns, or now when None. Raises ``ApiProblem`` 422
    naming ``loc`` for text that is not a time."""
    if t is None or not str(t).strip():
        return int(now_ns if now_ns is not None else _now_ns())
    try:
        return ns_from_iso(str(t).strip())
    except (ValueError, TypeError) as exc:
        raise ApiProblem(422, f"{loc}: not an ISO 8601 time ({exc})",
                         errors=[{"loc": loc, "msg": f"not an ISO 8601 time: {exc}"}]) from exc


async def _read_body(request: Request) -> bytes:
    """The request body, refusing more than ``MAX_BODY_BYTES`` (413) and nothing (422)."""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > MAX_BODY_BYTES:
                raise ApiProblem(413, f"the request body is larger than {MAX_BODY_BYTES} bytes")
        except ValueError:
            raise ApiProblem(400, "Content-Length is not a number") from None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise ApiProblem(413, f"the request body is larger than {MAX_BODY_BYTES} bytes")
        chunks.append(chunk)
    body = b"".join(chunks)
    if not body.strip():
        raise ApiProblem(422, "the request has no body", errors=[{"loc": "(root)", "msg": "the request has no body"}])
    return body


async def _read_document(request: Request, envelope_keys: frozenset[str], inner: str) -> tuple[str | dict[str, Any], str]:
    """A YAML or JSON document from the request body.

    The format follows ``Content-Type`` (``application/json``, ``application/yaml`` and
    their aliases); with no type, ``text/plain`` or ``application/octet-stream`` it is
    sniffed (a body starting with ``{`` is JSON). A JSON envelope as the server returns
    it (``{version, <inner>, ...}``) is unwrapped. Returns ``(text or object, fmt)``.
    Raises ``ApiProblem`` 415 for another type, 413 / 422 for the body itself.
    """
    raw = request.headers.get("content-type", "")
    ctype = raw.split(";", 1)[0].strip().lower()
    body = await _read_body(request)
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise ApiProblem(422, "the request body is not UTF-8 text",
                         errors=[{"loc": "(root)", "msg": "not UTF-8 text"}]) from None
    if ctype in _JSON_TYPES or ctype.endswith("+json"):
        fmt = "json"
    elif ctype in _YAML_TYPES or ctype.endswith("+yaml"):
        fmt = "yaml"
    elif ctype in _SNIFF_TYPES:
        fmt = "json" if text.lstrip().startswith("{") else "yaml"
    else:
        raise ApiProblem(415, f"Content-Type {raw!r} is not supported: send application/json or application/yaml")
    if fmt == "json":
        try:
            parsed = json.loads(text)
        except ValueError:
            return text, fmt  # the loader reports the syntax error with its position
        if isinstance(parsed, dict) and inner in parsed and set(parsed) <= envelope_keys:
            document = parsed[inner]
            if not isinstance(document, dict):
                raise ApiProblem(422, f"{inner}: must be an object", errors=[{"loc": inner, "msg": "must be an object"}])
            return document, fmt
    return text, fmt


def _openapi_document_body(schema_name: str, what: str) -> dict[str, Any]:
    return {
        "requestBody": {
            "required": True,
            "description": f"The {what} as JSON (or a JSON envelope as returned by the server) or as YAML.",
            "content": {
                "application/json": {"schema": {"$ref": f"#/components/schemas/{schema_name}"}},
                "application/yaml": {"schema": {"type": "string"}},
            },
        }
    }


# --------------------------------------------------------------------------- geometry helpers (pure)


def _position(orbit: Any, t_s: float, sat_id: str) -> np.ndarray | None:
    try:
        return np.asarray(orbit_position(orbit, t_s), dtype=np.float64)
    except ValueError as exc:
        logger.warning("satellite %s cannot be placed at t = %.3f s: %s", sat_id, t_s, exc,
                       extra={"sat_id": sat_id})
        return None


def compute_subpoints(cfg: SystemConfig, t_ns: int) -> list[schemas.Subpoint]:
    """Sub-satellite point of every satellite at ``t_ns`` (longitudes in [-180, 180)).
    A satellite whose orbit cannot be evaluated then (an ephemeris table that does not
    cover ``t_ns``) is left out and logged."""
    t_s = sec(t_ns)
    out: list[schemas.Subpoint] = []
    orbits = cfg.orbits()
    for sat in cfg.satellites:
        try:
            lat, lon, alt = subsatellite_point(orbits[sat.id], t_s)
        except ValueError as exc:
            logger.warning("no sub-point for satellite %s: %s", sat.id, exc, extra={"sat_id": sat.id})
            continue
        lat_f, lon_f, alt_f = _finite(lat), _finite(wrap_lon_deg(float(lon))), _finite(alt)
        if lat_f is None or lon_f is None or alt_f is None:
            logger.warning("no sub-point for satellite %s: not finite", sat.id, extra={"sat_id": sat.id})
            continue
        out.append(schemas.Subpoint(sat_id=sat.id, lat_deg=lat_f, lon_deg=lon_f, alt_m=alt_f))
    return out


def compute_footprints(cfg: SystemConfig, t_ns: int, min_elevation_deg: float = 0.0) -> list[schemas.Footprint]:
    """Footprint ring of every satellite at ``t_ns`` above ``min_elevation_deg``
    (``[lon, lat]`` pairs, longitudes continuous around the sub-satellite longitude).
    Raises ValueError for an elevation outside (-90, 90); a satellite that cannot be
    placed is left out and logged."""
    if not -90.0 < float(min_elevation_deg) < 90.0:
        raise ValueError(f"min_elevation_deg must be within (-90, 90), got {min_elevation_deg!r}")
    t_s = sec(t_ns)
    orbits = cfg.orbits()
    out: list[schemas.Footprint] = []
    for sat in cfg.satellites:
        try:
            ring = footprint(orbits[sat.id], t_s, min_elevation_deg=float(min_elevation_deg))
        except ValueError as exc:
            logger.warning("no footprint for satellite %s: %s", sat.id, exc, extra={"sat_id": sat.id})
            continue
        points = [[round(float(lon), 5), round(float(lat), 5)] for lat, lon in ring]
        if not all(math.isfinite(v) for p in points for v in p):
            logger.warning("no footprint for satellite %s: not finite", sat.id, extra={"sat_id": sat.id})
            continue
        out.append(schemas.Footprint(sat_id=sat.id, min_elevation_deg=float(min_elevation_deg), ring=points))
    return out


def _look_frequency(sat: Any, channel: Any) -> float | None:
    if sat.transponders:
        return float(sat.transponders[0].downlink_center_hz)
    if channel.monitors:
        return float(channel.monitors[0].center_hz)
    return None


def compute_look_angles(cfg: SystemConfig, t_ns: int) -> list[schemas.LookAngle]:
    """Every receive channel looking at every satellite at ``t_ns``: azimuth,
    elevation, range, the angle off the channel's boresight, the antenna gain toward
    the satellite at the downlink centre of its first transponder, the boresight gain
    there and the electrically-small flag. Rows that cannot be computed (an orbit not
    defined at ``t_ns``) are left out and logged."""
    t_s = sec(t_ns)
    orbits = cfg.orbits()
    positions = {sat.id: _position(orbits[sat.id], t_s, sat.id) for sat in cfg.satellites}
    rows: list[schemas.LookAngle] = []
    for site in cfg.sites:
        xyz = site.xyz()
        for channel in site.channels:
            try:
                boresight = np.asarray(cfg.boresight_ecef(channel.id, t_s), dtype=np.float64)
            except ValueError as exc:
                logger.warning("no boresight for channel %s: %s", channel.id, exc, extra={"channel_id": channel.id})
                continue
            watched = set(channel.watched_satellite_ids)
            for sat in cfg.satellites:
                r = positions.get(sat.id)
                freq = _look_frequency(sat, channel)
                if r is None or freq is None:
                    continue
                az, el, rng = look_angles(xyz, r)
                off = angle_between_deg(boresight, r - xyz)
                values = {
                    "az_deg": _finite(az),
                    "el_deg": _finite(el),
                    "range_km": _finite(float(rng) / 1000.0),
                    "off_axis_deg": _finite(off),
                    "gain_dbi": _finite(gain_dbi(channel.antenna, float(off), freq)),
                    "peak_gain_dbi": _finite(gain_dbi(channel.antenna, 0.0, freq)),
                }
                if any(v is None for v in values.values()):
                    logger.warning("look angle %s -> %s not finite", channel.id, sat.id,
                                   extra={"channel_id": channel.id, "sat_id": sat.id})
                    continue
                rows.append(
                    schemas.LookAngle(
                        site_id=site.id,
                        channel_id=channel.id,
                        sat_id=sat.id,
                        freq_hz=freq,
                        visible=bool(values["el_deg"] > 0.0),  # type: ignore[operator]
                        watched=sat.id in watched,
                        electrically_small=bool(electrically_small(channel.antenna, freq)),
                        **values,  # type: ignore[arg-type]
                    )
                )
    return rows


def _covering_monitor(channel: Any, f_hz: float) -> Any | None:
    """The monitor window of a channel that covers ``f_hz`` (the finest bins first)."""
    covering = [m for m in channel.monitors if m.center_hz - 0.5 * m.span_hz <= f_hz <= m.center_hz + 0.5 * m.span_hz]
    return min(covering, key=lambda m: m.bin_hz) if covering else None


def compute_link_budget(
    cfg: SystemConfig,
    emitter: EmitterModel,
    f_e_hz: float,
    lat_deg: float,
    lon_deg: float,
    alt_m: float,
    t_ns: int,
) -> list[schemas.LinkBudgetRow]:
    """The emitter relayed by every transponder whose uplink passband holds ``f_e_hz``
    into every channel watching that satellite, at ``t_ns``. ``alt_m`` is the height
    of the emitter's phase centre above the WGS84 ellipsoid. Paths that do not exist
    (a satellite below either horizon) are left out."""
    x_e = lla_to_ecef(float(lat_deg), float(lon_deg), float(alt_m))
    t_s = sec(t_ns)
    bandwidth = float(emitter.bandwidth_hz) if emitter.bandwidth_hz > 0 else 1.0
    threshold = float(cfg.processing.detection.threshold_db)
    rows: list[schemas.LinkBudgetRow] = []
    for sat in cfg.satellites:
        for transponder in sat.transponders:
            if not transponder.relays(float(f_e_hz)):
                continue
            f_down = float(f_e_hz) + float(transponder.translation_hz)
            for site, channel in cfg.channels_watching(sat.id):
                budget = path_budget(cfg, emitter, float(f_e_hz), x_e, sat.id, transponder.id, channel.id, t_s)
                if budget is None:
                    continue
                cn0 = _finite(budget.cn0_total_dbhz)
                values = {
                    "uplink_elevation_deg": _finite(budget.uplink.elevation_deg),
                    "eirp_toward_sat_dbw": _finite(budget.uplink.eirp_toward_sat_dbw),
                    "pfd_dbw_m2": _finite(budget.uplink.pfd_dbw_m2),
                    "cn0_up_dbhz": _finite(budget.uplink.cn0_up_dbhz),
                    "transponder_eirp_dbw": _finite(budget.transponder_eirp_dbw),
                    "downlink_elevation_deg": _finite(budget.downlink.elevation_deg),
                    "off_axis_deg": _finite(budget.downlink.off_axis_deg),
                    "c_rx_dbw": _finite(budget.downlink.c_rx_dbw),
                    "cn0_total_dbhz": cn0,
                    "snr_db": None if cn0 is None else cn0 - 10.0 * math.log10(bandwidth),
                }
                if any(v is None for v in values.values()):
                    logger.warning("link budget %s/%s -> %s not finite", sat.id, transponder.id, channel.id,
                                   extra={"sat_id": sat.id, "channel_id": channel.id})
                    continue
                monitor = _covering_monitor(channel, f_down)
                detectable = False
                if monitor is not None and cn0 is not None:
                    snr_bin_db = cn0 - 10.0 * math.log10(max(bandwidth, float(monitor.bin_hz)))
                    rise_db = 10.0 * math.log10(1.0 + 10.0 ** (snr_bin_db / 10.0))
                    detectable = rise_db >= threshold
                rows.append(
                    schemas.LinkBudgetRow(
                        sat_id=sat.id,
                        transponder_id=transponder.id,
                        site_id=site.id,
                        channel_id=channel.id,
                        f_down_hz=f_down,
                        detectable=detectable,
                        monitor_id=None if monitor is None else monitor.id,
                        **values,  # type: ignore[arg-type]
                    )
                )
    return rows


# --------------------------------------------------------------------------- accuracy (banded)


def accuracy_bands(region: Region, step_deg: float, band_cells: int | None = None
                   ) -> tuple[np.ndarray, np.ndarray, list[Region]]:
    """Split the accuracy grid of ``region`` into latitude bands.

    Returns ``(lat_deg, lon_deg, bands)``: the full grid axes of ``region.grid(step_deg)``
    and sub-regions whose grids are consecutive row blocks of it (each at least two
    rows, about ``band_cells`` cells, default ``ACCURACY_BAND_CELLS``; one band when the
    grid has a single row)."""
    lat, lon = region.grid(step_deg)
    n_lat, n_lon = int(lat.size), int(lon.size)
    if n_lat < 2:
        return lat, lon, [region]
    target = ACCURACY_BAND_CELLS if band_cells is None else int(band_cells)
    rows = max(2, math.ceil(target / max(n_lon, 1)))
    edges = list(range(0, n_lat, rows)) + [n_lat]
    if edges[-1] - edges[-2] < 2 and len(edges) > 2:
        edges.pop(-2)  # a one-row tail joins the band before it
    bands: list[Region] = []
    for start, stop in zip(edges[:-1], edges[1:]):
        bands.append(
            Region(
                lat_min=float(lat[start]),
                lat_max=float(lat[stop - 1]),
                lon_min=region.lon_min,
                lon_max=region.lon_max,
            )
        )
    return lat, lon, bands


def _grid_part(values: Any, n_rows: int, n_lon: int, name: str, *, text: bool = False) -> list[list[Any]]:
    """One grid of an ``accuracy_map`` result as rows of finite floats (or strings) and
    None. Raises ValueError when the shape is not ``[n_rows][n_lon]``."""
    if values is None:
        return [[None] * n_lon for _ in range(n_rows)]
    rows = list(values)
    if len(rows) != n_rows:
        raise ValueError(f"accuracy_map returned {len(rows)} rows of {name}, expected {n_rows}")
    out: list[list[Any]] = []
    for row in rows:
        cells = list(row)
        if len(cells) != n_lon:
            raise ValueError(f"accuracy_map returned {len(cells)} columns of {name}, expected {n_lon}")
        if text:
            out.append([None if c is None else str(c) for c in cells])
        else:
            out.append([_finite(c) for c in cells])
    return out


def compute_accuracy(
    accuracy_map: Callable[..., Mapping[str, Any]],
    cfg: SystemConfig,
    request: schemas.AccuracyRequest,
    t_ns: int,
    *,
    progress: Callable[[int, int], None] | None = None,
    should_stop: Callable[[], bool] = lambda: False,
) -> schemas.AccuracyGrid:
    """Synchronous banded accuracy map (the command line uses it; the API runs the
    bands on the worker pool instead).

    accuracy_map: ``core.accuracy.accuracy_map``. request: as for the API (its ``t`` is
    ignored; ``t_ns`` is used). progress: called with (bands done, bands) after each
    band. should_stop: asked before each band; ``TimeoutError`` when it returns True.
    Raises ValueError from ``accuracy_map`` or when its result does not follow the grid.
    """
    region = request.region or cfg.processing.region
    lat, lon, bands = accuracy_bands(region, request.step_deg)
    parts = []
    for band in bands:
        if should_stop():
            raise TimeoutError("the accuracy map was stopped")
        parts.append(_accuracy_call(accuracy_map, cfg, request, t_ns, band))
        if progress is not None:
            progress(len(parts), len(bands))
    return _merge_accuracy(t_ns, lat, lon, bands, request.step_deg, parts)


def accuracy_config(cfg: SystemConfig, request: schemas.AccuracyRequest) -> SystemConfig:
    """The configuration an accuracy map is computed with: ``cfg`` itself, or a copy
    whose ``processing.uncertainty`` is the request's override (the configuration in
    force is never changed)."""
    if request.uncertainty is None:
        return cfg
    processing = cfg.processing.model_copy(update={"uncertainty": request.uncertainty})
    return cfg.model_copy(update={"processing": processing})


def _accuracy_call(accuracy_map: Callable[..., Mapping[str, Any]], cfg: SystemConfig,
                   request: schemas.AccuracyRequest, t_ns: int, band: Region) -> Mapping[str, Any]:
    return accuracy_map(
        accuracy_config(cfg, request),
        [tuple(p) for p in request.pairs],
        request.emitter,
        float(request.f_e_hz),
        int(t_ns),
        band,
        float(request.step_deg),
        kinds=tuple(request.kinds),
        reference_carrier_id=request.reference_carrier_id,
        integration_s=request.integration_s,
    )


def _merge_accuracy(t_ns: int, lat: np.ndarray, lon: np.ndarray, bands: Sequence[Region], step: float,
                    parts: Sequence[Mapping[str, Any]]) -> schemas.AccuracyGrid:
    n_lon = int(lon.size)
    semi: list[list[float | None]] = []
    cep: list[list[float | None]] = []
    dominant: list[list[str | None]] = []
    row = 0
    for band, part in zip(bands, parts):
        part_lat = np.asarray(part.get("lat_deg", ()), dtype=np.float64).reshape(-1)
        part_lon = np.asarray(part.get("lon_deg", ()), dtype=np.float64).reshape(-1)
        n_rows = int(part_lat.size)
        expected = lat[row:row + n_rows]
        if n_rows == 0 or expected.size != n_rows or not np.allclose(part_lat, expected, atol=1e-6 * max(step, 1.0)):
            raise ValueError(f"accuracy_map returned latitudes that do not follow the grid of band "
                             f"{band.lat_min:g}..{band.lat_max:g}")
        if part_lon.size != n_lon or not np.allclose(part_lon, lon, atol=1e-6 * max(step, 1.0)):
            raise ValueError("accuracy_map returned longitudes that do not follow the grid")
        semi += _grid_part(part.get("semi_major_95_m"), n_rows, n_lon, "semi_major_95_m")
        cep += _grid_part(part.get("cep50_m"), n_rows, n_lon, "cep50_m")
        dominant += _grid_part(part.get("dominant"), n_rows, n_lon, "dominant", text=True)
        row += n_rows
    if row != lat.size:
        raise ValueError(f"accuracy_map covered {row} of {lat.size} grid rows")
    return schemas.AccuracyGrid(
        t_ns=int(t_ns),
        lat_deg=[float(v) for v in lat],
        lon_deg=[float(v) for v in lon],
        semi_major_95_m=semi,
        cep50_m=cep,
        dominant=dominant,
    )


def _load_accuracy_map() -> Callable[..., Mapping[str, Any]]:
    """``core.accuracy.accuracy_map``; ``ApiProblem`` 501 when this build lacks it."""
    try:
        from petal_locate.core.accuracy import accuracy_map
    except ImportError as exc:
        logger.error("accuracy prediction is not available: %s", exc)
        raise ApiProblem(501, "accuracy prediction is not available in this build (core.accuracy is missing)") from exc
    return accuracy_map


def accuracy_problems(cfg: SystemConfig, request: schemas.AccuracyRequest) -> list[dict[str, str]]:
    channels = {channel.id for site in cfg.sites for channel in site.channels}
    errors: list[dict[str, str]] = []
    for i, (a, b) in enumerate(request.pairs):
        for j, channel_id in enumerate((a, b)):
            if channel_id not in channels:
                errors.append({"loc": f"pairs.{i}.{j}", "msg": f"unknown receive channel {channel_id!r}"})
        if a == b:
            errors.append({"loc": f"pairs.{i}", "msg": "the two channels of a pair must differ"})
    if request.reference_carrier_id is not None:
        try:
            carrier = cfg.carrier(request.reference_carrier_id)
        except ValueError:
            errors.append({"loc": "reference_carrier_id", "msg": f"unknown carrier {request.reference_carrier_id!r}"})
        else:
            if not carrier.use_as_reference:
                errors.append({"loc": "reference_carrier_id",
                               "msg": f"carrier {carrier.id!r} is not a reference carrier (use_as_reference is false)"})
    return errors


# --------------------------------------------------------------------------- lines of position


def lop_step_deg(region: Region, max_cells: int = LOP_MAX_CELLS, min_step: float = LOP_MIN_STEP_DEG) -> float:
    """Grid step for contouring lines of position over ``region``: the finest multiple
    of 0.05 degrees, not below ``min_step``, whose grid has at most ``max_cells`` cells."""
    lat_span = float(region.lat_max - region.lat_min)
    lon_span = float(region.lon_span_deg())
    step = max(min_step, math.sqrt(max(lat_span * lon_span, 0.0) / max_cells))
    step = math.ceil(step / 0.05 - 1e-9) * 0.05
    while (lat_span / step + 2) * (lon_span / step + 2) > max_cells:
        step += 0.05
    return round(step, 6)


def _latest_used(record: EventRecord) -> list[MeasurementEntry]:
    """The newest used measurement of each (kind, channel pair, satellite pair)."""
    latest: dict[tuple[str, ...], MeasurementEntry] = {}
    for entry in record.used_measurements():
        pair = entry.row.pair
        key = (entry.row.kind, pair.a.channel_id, pair.a.sat_id, pair.b.channel_id, pair.b.sat_id)
        if key not in latest or entry.seq > latest[key].seq:
            latest[key] = entry
    return sorted(latest.values(), key=lambda e: e.seq)


def _lop_features(entry: MeasurementEntry, event_id: str, cfg: SystemConfig, step: float) -> list[schemas.LopFeature]:
    """Lines of position of one measurement (blocking; runs on the worker pool)."""
    from petal_locate.core.lop import lop_geojson

    measurement = entry.measurement
    if measurement is None:
        return []
    solver = cfg.processing.solver
    collection = lop_geojson(
        measurement,
        cfg.orbits(),
        cfg.sites_xyz(),
        cfg.processing.region,
        height_m=float(solver.height_m),
        min_elevation_deg=float(solver.min_elevation_deg),
        step_deg=step,
        k_sigma=LOP_K_SIGMA,
    )
    pair = entry.row.pair
    features: list[schemas.LopFeature] = []
    for feature in collection.get("features", ()):
        props = feature.get("properties", {})
        geometry = feature.get("geometry")
        if not isinstance(geometry, dict):
            continue
        try:
            json.dumps(geometry, allow_nan=False)
            properties = schemas.LopProperties(
                role=props["role"],
                kind=props["kind"],
                event_id=event_id,
                seq=entry.seq,
                tag=str(props.get("tag") or ""),
                a_sat_id=pair.a.sat_id,
                a_site_id=pair.a.site_id,
                a_channel_id=pair.a.channel_id,
                b_sat_id=pair.b.sat_id,
                b_site_id=pair.b.site_id,
                b_channel_id=pair.b.channel_id,
                t_ns=int(props["t_ns"]),
                value=float(props["value"]),
                correction=float(props["correction"]),
                calibrated=bool(props["calibrated"]),
                sigma=float(props["sigma"]),
                sigma_total=float(props["sigma_total"]),
                k_sigma=float(props["k_sigma"]),
                height_m=float(props["height_m"]),
            )
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            logger.warning("event %s: a line of position of measurement %d is not reportable: %s",
                           event_id, entry.seq, exc, extra={"event_id": event_id})
            continue
        features.append(schemas.LopFeature(geometry=geometry, properties=properties))
    return features


# --------------------------------------------------------------------------- simulation truth


def sim_truth(runtime: ServerRuntime, now_ns: int | None = None) -> schemas.SimTruth:
    """``GET /sim/truth``: every simulated emitter with its defaults resolved, and the
    miss distance of each active event's estimate. Raises ``NotSimulating``."""
    scenario = runtime.scenario
    if scenario is None:
        raise NotSimulating()
    cfg = runtime.config
    now = int(now_ns if now_ns is not None else _now_ns())
    truth = resolve_truth_emitters(scenario, cfg)
    specs = {e.id: e for e in scenario.emitters}
    carriers = {c.id: c for c in cfg.carriers}
    emitters: list[schemas.SimTruthEmitter] = []
    for te in truth:
        spec = specs.get(te.id)
        if spec is not None:
            carrier = carriers.get(spec.carrier_id) if spec.carrier_id is not None else None
            model = carrier.emitter if carrier is not None else None
            name = spec.name or (carrier.name if carrier is not None else te.id)
            eirp = spec.eirp_dbw if spec.eirp_dbw is not None else (model.eirp_dbw if model is not None else None)
            waveform = spec.waveform.type if spec.waveform is not None else (model.waveform if model is not None else None)
            active = spec.enabled
            if spec.t_start is not None and now < ns_from_iso(spec.t_start):
                active = False
            if spec.t_stop is not None and now >= ns_from_iso(spec.t_stop):
                active = False
        else:
            carrier = carriers.get(te.id)
            model = carrier.emitter if carrier is not None else None
            name = carrier.name if carrier is not None else te.id
            eirp = model.eirp_dbw if model is not None else None
            waveform = model.waveform if model is not None else None
            active = te.enabled
        lat, lon, freq, bw, eirp_f = (_finite(te.lat_deg), _finite(te.lon_deg), _finite(te.freq_hz),
                                      _finite(te.bandwidth_hz), _finite(eirp))
        if None in (lat, lon, freq, bw, eirp_f) or waveform is None:
            logger.warning("simulated emitter %s is not reportable (missing or non-finite values)", te.id,
                           extra={"emitter_id": te.id})
            continue
        emitters.append(
            schemas.SimTruthEmitter(
                id=te.id,
                name=name,
                kind=te.kind,  # type: ignore[arg-type]
                lat_deg=lat,  # type: ignore[arg-type]
                lon_deg=lon,  # type: ignore[arg-type]
                freq_hz=freq,  # type: ignore[arg-type]
                eirp_dbw=eirp_f,  # type: ignore[arg-type]
                bandwidth_hz=bw,  # type: ignore[arg-type]
                waveform=str(waveform),
                enabled=te.enabled,
                active=bool(active),
            )
        )
    events: list[schemas.SimTruthEvent] = []
    for record in runtime.events.active():
        match = event_truth(record, truth)
        events.append(
            schemas.SimTruthEvent(
                event_id=record.id,
                emitter_id=None if match is None else match.emitter_id,
                miss_m=None if match is None else match.miss_m,
                inside_ellipse=None if match is None else match.inside_ellipse,
            )
        )
    return schemas.SimTruth(version=runtime.scenario_version, emitters=emitters, events=events)


# --------------------------------------------------------------------------- routers

public_router = APIRouter(
    prefix=API_PREFIX,
    tags=["meta"],
    responses={422: {"model": schemas.FieldErrorResponse}, 503: {"model": schemas.ErrorResponse}},
)
"""Routes readable without the API token."""

api_router = APIRouter(prefix=API_PREFIX, dependencies=[Depends(require_api_token)], responses=_AUTH)
"""Every other console route (API token required when configured)."""

backplane_router = APIRouter(tags=["simulation backplane"])
"""The node simulation backplane (node token)."""


@public_router.get("/meta", response_model=schemas.Meta, responses={503: {"model": schemas.ErrorResponse}})
async def get_meta(request: Request) -> Response:
    """Server version, marking banner, whether the server simulates, server time,
    configuration name and whether an API token is required. No token needed."""
    return _json(get_runtime(request).meta())


# ----------------------------------------------------------------- configuration


@api_router.get(
    "/config",
    tags=["configuration"],
    response_model=schemas.ConfigEnvelope,
    responses={200: {"content": {"application/yaml": {"schema": {"type": "string"}}},
                     "description": "The envelope as JSON, or with format=yaml the configuration as YAML. "
                                    "The ETag header is the version."},
               **_ERRORS_422},
)
async def get_config(
    request: Request,
    format: str = Query("json", pattern="^(json|yaml)$", description="json (envelope) or yaml (export)"),
) -> Response:
    """The configuration in force with its version (also the quoted ``ETag``) and its
    current warnings; ``?format=yaml`` exports it as a YAML file instead (derived
    monitors left out, so they follow the frequency plan when imported again)."""
    runtime = get_runtime(request)
    cfg, version = runtime.config, runtime.config_version
    headers = {"ETag": _etag(version), "Cache-Control": "no-cache"}
    if format == "yaml":
        text = await runtime.pool.run(dump_config, cfg)
        headers["Content-Disposition"] = f'attachment; filename="{_safe_name(cfg.name)}-v{version}.yaml"'
        return Response(text, media_type="application/yaml", headers=headers)
    envelope = runtime.config_envelope()
    try:
        warnings = await runtime.pool.run(cfg.warnings, _now_ns() / 1e9)
    except ValueError as exc:
        logger.warning("configuration warnings could not be computed: %s", exc)
        warnings = []
    envelope = envelope.model_copy(
        update={"warnings": [schemas.FieldProblem(loc=w["loc"], msg=w["msg"]) for w in warnings]}
    )
    return _json(envelope, headers=headers)


def _safe_name(name: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in "-_." else "-" for c in name).strip("-.")
    return cleaned[:64] or "config"


@api_router.put(
    "/config",
    tags=["configuration"],
    response_model=schemas.ConfigEnvelope,
    responses={409: {"model": schemas.ConflictResponse}, 413: {"model": schemas.ErrorResponse},
               415: {"model": schemas.ErrorResponse}, 428: {"model": schemas.ErrorResponse},
               500: {"model": schemas.ErrorResponse}, **_ERRORS_422},
    openapi_extra=_openapi_document_body("SystemConfig", "configuration"),
)
async def put_config(request: Request) -> Response:
    """Replace the configuration. ``If-Match`` must carry the version loaded (the
    ``ETag``; ``*`` for any): 428 without it, 409 with the current version when it is
    stale, 422 with each field at fault. The body's own ``version`` is ignored; the
    reply carries the new version and its warnings."""
    runtime = get_runtime(request)
    if_match = request.headers.get("if-match")
    if if_match is None:
        raise PreconditionRequired("If-Match is required: send the version you loaded (the ETag of GET /config)")
    document, fmt = await _read_document(request, _CONFIG_ENVELOPE_KEYS, "config")
    envelope = await runtime.replace_config(document, if_match, fmt=fmt)
    return _json(envelope, headers={"ETag": _etag(envelope.version)})


@api_router.post(
    "/config/validate",
    tags=["configuration"],
    response_model=schemas.ValidateResult,
    responses={413: {"model": schemas.ErrorResponse}, 415: {"model": schemas.ErrorResponse}},
    openapi_extra=_openapi_document_body("SystemConfig", "configuration"),
)
async def validate_config(request: Request) -> Response:
    """Validate a configuration without applying it: field errors and warnings (always
    HTTP 200; ``ok`` says whether it would be accepted)."""
    runtime = get_runtime(request)
    document, fmt = await _read_document(request, _CONFIG_ENVELOPE_KEYS, "config")
    return _json(await runtime.validate_config(document, fmt=fmt))


@api_router.get("/config/schema", tags=["configuration"], response_model=dict[str, Any])
async def get_config_schema(request: Request) -> Response:
    """JSON Schema (draft 2020-12) of the configuration document."""
    state = _api_state(request)
    if state.config_schema is None:
        runtime = get_runtime(request)
        state.config_schema = await runtime.pool.run(lambda: json.dumps(config_json_schema()).encode("utf-8"))
    return Response(state.config_schema, media_type="application/json")


# ----------------------------------------------------------------- status and events


@api_router.get("/status", tags=["status"], response_model=schemas.Status)
async def get_status(request: Request) -> Response:
    """Server time, configuration version, every configured site's node, the number of
    active events and the work in hand."""
    return _json(get_runtime(request).status())


@api_router.get("/events", tags=["events"], response_model=list[schemas.EventSummary], responses=_ERRORS_422)
async def list_events(
    request: Request,
    state: str | None = Query(
        None,
        description=(
            "one value: detected, characterizing, locating, located, lost, closed, or active "
            "(detected, characterizing, locating and located together); absent for every event. "
            "Any other value is a 422."
        ),
    ),
    limit: int = Query(100, ge=1, le=1000),
) -> Response:
    """Events, newest first (by when they were raised). ``state`` filters on one
    lifecycle state or on ``active``; ``limit`` caps the list (default 100)."""
    runtime = get_runtime(request)
    try:
        summaries = runtime.list_events(state=state, limit=limit)
    except ValueError as exc:
        raise ApiProblem(422, str(exc), errors=[{"loc": "state", "msg": str(exc)}]) from exc
    return _json_list(_SUMMARIES, summaries)


@api_router.get("/events/{event_id}", tags=["events"], response_model=schemas.Event, responses=_ERRORS_404)
async def get_event(request: Request, event_id: str) -> Response:
    """One event: evidence, verdict, transitions, measurements, solution and its
    history, the latest correlation surface and (simulation only) the truth."""
    return _json(get_runtime(request).get_event(event_id))


@api_router.post("/events/{event_id}/close", tags=["events"], response_model=schemas.Event, responses=_ERRORS_404)
async def close_event(request: Request, event_id: str) -> Response:
    """Close an event (operator). Closing a closed event changes nothing."""
    runtime = get_runtime(request)
    event = runtime.close_event(event_id)
    logger.info("event %s closed by operator", event_id, extra={"event_id": event_id})
    return _json(event)


@api_router.get(
    "/events/{event_id}/lop",
    tags=["events"],
    response_model=schemas.LopCollection,
    responses={**_ERRORS_404, **_ERRORS_422},
)
async def get_event_lop(
    request: Request,
    event_id: str,
    step_deg: float | None = Query(None, ge=0.05, le=5.0, description="grid step; automatic when absent"),
) -> Response:
    """Lines of position (and their 2-sigma bands) of the newest used measurement of
    each pair and kind, as GeoJSON over the configured region. Cached per solution."""
    runtime = get_runtime(request)
    record = runtime.events.get(event_id)
    if record is None:
        raise EventNotFound(event_id)
    cfg = runtime.config
    region = cfg.processing.region
    step = lop_step_deg(region) if step_deg is None else float(step_deg)
    lat, lon = region.grid(step)
    if lat.size * lon.size > 2 * LOP_MAX_CELLS:
        raise ApiProblem(422, f"step_deg: a {step:g} degree grid over the region has {lat.size * lon.size} cells, "
                              f"more than {2 * LOP_MAX_CELLS}; choose a coarser step",
                         errors=[{"loc": "step_deg", "msg": "grid too fine for the region"}])
    entries = _latest_used(record)
    key = (record.id, record.solution_t_ns, tuple(e.seq for e in entries), runtime.config_version, step)
    state = _api_state(request)
    cached = state.lop_cache.get(key)
    if cached is not None:
        state.lop_cache.move_to_end(key)
        return Response(cached, media_type="application/json")
    features: list[schemas.LopFeature] = []
    for entry in entries:
        try:
            features += await runtime.pool.run(_lop_features, entry, record.id, cfg, step)
        except (ValueError, KeyError) as exc:
            logger.warning("event %s: no line of position for measurement %d: %s", record.id, entry.seq, exc,
                           extra={"event_id": record.id})
    body = await runtime.pool.run(lambda: schemas.LopCollection(features=features).model_dump_json().encode("utf-8"))
    _remember(state.lop_cache, key, body, LOP_CACHE_SIZE)
    return Response(body, media_type="application/json")


# ----------------------------------------------------------------- geometry


@api_router.get("/geometry/subpoints", tags=["geometry"], response_model=list[schemas.Subpoint],
                responses=_ERRORS_422)
async def get_subpoints(request: Request, t: str | None = Query(None, description="ISO 8601 UTC; default now")
                        ) -> Response:
    """Sub-satellite point of every satellite."""
    runtime = get_runtime(request)
    t_ns = resolve_time_ns(t)
    return _json_list(_SUBPOINTS, await runtime.pool.run(compute_subpoints, runtime.config, t_ns))


@api_router.get("/geometry/footprints", tags=["geometry"], response_model=list[schemas.Footprint],
                responses=_ERRORS_422)
async def get_footprints(
    request: Request,
    t: str | None = Query(None, description="ISO 8601 UTC; default now"),
    min_elevation_deg: float = Query(0.0, gt=-90.0, lt=90.0),
) -> Response:
    """Where each satellite is above ``min_elevation_deg``."""
    runtime = get_runtime(request)
    t_ns = resolve_time_ns(t)
    rings = await runtime.pool.run(compute_footprints, runtime.config, t_ns, min_elevation_deg)
    return _json_list(_FOOTPRINTS, rings)


@api_router.get("/geometry/look-angles", tags=["geometry"], response_model=list[schemas.LookAngle],
                responses=_ERRORS_422)
async def get_look_angles(request: Request, t: str | None = Query(None, description="ISO 8601 UTC; default now")
                          ) -> Response:
    """Every receive channel by every satellite: azimuth, elevation, range, off-axis
    angle, antenna gain toward the satellite and the electrically-small flag."""
    runtime = get_runtime(request)
    t_ns = resolve_time_ns(t)
    return _json_list(_LOOK_ANGLES, await runtime.pool.run(compute_look_angles, runtime.config, t_ns))


# ----------------------------------------------------------------- analysis


@api_router.post(
    "/analysis/accuracy",
    tags=["analysis"],
    response_model=schemas.AccuracyGrid,
    responses={**_ERRORS_422, 501: {"model": schemas.ErrorResponse}, 503: {"model": schemas.ErrorResponse},
               504: {"model": schemas.ErrorResponse}},
)
async def post_accuracy(request: Request, body: schemas.AccuracyRequest) -> Response:
    """Predicted 95 % error (semi-major axis), CEP50 and dominant error source over a
    grid, for the given channel pairs and emitter assumptions (MATH.md section 10)."""
    runtime = get_runtime(request)
    cfg, version = runtime.config, runtime.config_version
    errors = accuracy_problems(cfg, body)
    if errors:
        raise ApiProblem(422, _summary(errors, "accuracy request"), errors=errors)
    region = body.region or cfg.processing.region
    lat, lon, bands = accuracy_bands(region, body.step_deg)
    cells = int(lat.size * lon.size)
    if cells > ACCURACY_MAX_CELLS:
        message = (f"a {body.step_deg:g} degree grid over the region has {cells} cells, more than "
                   f"{ACCURACY_MAX_CELLS}; choose a coarser step_deg or a smaller region")
        raise ApiProblem(422, message, errors=[{"loc": "step_deg", "msg": message}])
    now = _now_ns()
    t_ns = resolve_time_ns(body.t, now_ns=now - now % 60_000_000_000)
    key = (version, body.model_dump_json(exclude={"t"}), t_ns)
    state = _api_state(request)
    cached = state.accuracy_cache.get(key)
    if cached is not None:
        state.accuracy_cache.move_to_end(key)
        return Response(cached, media_type="application/json")
    accuracy_map = _load_accuracy_map()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + ACCURACY_TIMEOUT_S
    gate = state.gate()
    if gate.locked():  # one map at a time; a few may wait for their turn
        if state.accuracy_waiting >= ACCURACY_MAX_WAITING:
            raise ApiProblem(503, "too many accuracy maps are waiting; try again shortly",
                             headers={"Retry-After": "10"})
        state.accuracy_waiting += 1
        try:
            await asyncio.wait_for(gate.acquire(), timeout=max(deadline - loop.time(), 0.001))
        except TimeoutError:
            raise ApiProblem(503, "another accuracy map is still being computed; try again shortly",
                             headers={"Retry-After": "10"}) from None
        finally:
            state.accuracy_waiting -= 1
    else:
        await gate.acquire()
    started = time.perf_counter()
    try:
        parts = []
        for band in bands:
            if loop.time() > deadline:
                logger.warning("accuracy map stopped after %.0f s (%d of %d bands)", ACCURACY_TIMEOUT_S,
                               len(parts), len(bands), extra={"cells": cells})
                raise ApiProblem(504, f"the accuracy map took longer than {ACCURACY_TIMEOUT_S:g} s; choose a "
                                      "coarser step_deg or a smaller region")
            try:
                parts.append(await runtime.pool.run(_accuracy_call, accuracy_map, cfg, body, t_ns, band))
            except ValueError as exc:
                raise ApiProblem(422, f"the accuracy map cannot be computed: {exc}",
                                 errors=[{"loc": "(root)", "msg": str(exc)}]) from exc
        try:  # tens of thousands of cells: merged and serialised off the loop
            data = await runtime.pool.run(
                lambda: _merge_accuracy(t_ns, lat, lon, bands, body.step_deg, parts).model_dump_json().encode("utf-8")
            )
        except (ValueError, ValidationError) as exc:
            logger.error("accuracy map result unusable: %s", exc)
            raise ApiProblem(500, "the accuracy map result is unusable; see the server log") from exc
    finally:
        gate.release()
    logger.info("accuracy map: %d cells in %.1f s", cells, time.perf_counter() - started,
                extra={"cells": cells, "bands": len(bands)})
    _remember(state.accuracy_cache, key, data, ACCURACY_CACHE_SIZE)
    return Response(data, media_type="application/json")


@api_router.post("/analysis/link-budget", tags=["analysis"], response_model=schemas.LinkBudgetResponse,
                 responses=_ERRORS_422)
async def post_link_budget(request: Request, body: schemas.LinkBudgetRequest) -> Response:
    """An emitter at a position and frequency relayed by every transponder holding its
    frequency into every channel watching that satellite: the whole path budget, the
    resulting C/N0 and SNR, and whether the configured detection threshold is met."""
    runtime = get_runtime(request)
    cfg = runtime.config
    t_ns = resolve_time_ns(body.t)
    try:
        rows = await runtime.pool.run(compute_link_budget, cfg, body.emitter, body.f_e_hz, body.lat_deg,
                                      body.lon_deg, body.alt_m, t_ns)
    except ValueError as exc:
        raise ApiProblem(422, f"the link budget cannot be computed: {exc}",
                         errors=[{"loc": "(root)", "msg": str(exc)}]) from exc
    return _json(schemas.LinkBudgetResponse(t_ns=t_ns, rows=rows))


# ----------------------------------------------------------------- simulation


@api_router.get("/sim/scenario", tags=["simulation"], response_model=schemas.ScenarioEnvelope,
                responses=_ERRORS_404)
async def get_scenario(request: Request) -> Response:
    """The simulated world and its version (404 when the server is not simulating)."""
    return _json(get_runtime(request).scenario_envelope())


@api_router.put(
    "/sim/scenario",
    tags=["simulation"],
    response_model=schemas.ScenarioEnvelope,
    responses={**_ERRORS_404, **_ERRORS_422, 413: {"model": schemas.ErrorResponse},
               415: {"model": schemas.ErrorResponse}},
    openapi_extra=_openapi_document_body("Scenario", "scenario"),
)
async def put_scenario(request: Request) -> Response:
    """Replace the simulated world; the nodes are told with ``sim_update`` and switch
    2 s later."""
    runtime = get_runtime(request)
    if runtime.scenario is None:
        raise NotSimulating()
    document, _fmt = await _read_document(request, _SCENARIO_ENVELOPE_KEYS, "scenario")
    return _json(await runtime.replace_scenario(document, source="api"))


@api_router.get("/sim/presets", tags=["simulation"], response_model=list[schemas.Preset], responses=_ERRORS_404)
async def get_presets(request: Request) -> Response:
    """The preset scenarios that can be loaded."""
    return _json_list(_PRESETS, await get_runtime(request).presets())


@api_router.post("/sim/presets/{name}/load", tags=["simulation"], response_model=schemas.ScenarioEnvelope,
                 responses={**_ERRORS_404, **_ERRORS_422})
async def load_preset(request: Request, name: str) -> Response:
    """Load a preset scenario (announced to the nodes like ``PUT /sim/scenario``)."""
    return _json(await get_runtime(request).load_preset(name))


@api_router.post("/sim/reset", tags=["simulation"], response_model=schemas.ScenarioEnvelope, responses=_ERRORS_404)
async def reset_scenario(request: Request) -> Response:
    """Back to the scenario the server started with."""
    return _json(await get_runtime(request).reset_scenario())


@api_router.get("/sim/truth", tags=["simulation"], response_model=schemas.SimTruth, responses=_ERRORS_404)
async def get_truth(request: Request) -> Response:
    """Simulated emitters with their true positions and, per active event, the miss
    distance of the estimate (SIMULATION)."""
    return _json(sim_truth(get_runtime(request)))


# ----------------------------------------------------------------- node backplane


@backplane_router.get(
    SIM_BACKPLANE_PATH,
    responses={401: {"model": schemas.ErrorResponse}, 404: {"model": schemas.ErrorResponse},
               200: {"description": "{version, effective_ns (decimal string), config, scenario}"}},
)
async def get_backplane(request: Request) -> Response:
    """The simulated world for simulated nodes (PROTOCOL.md section A, simulation
    backplane): ``Authorization: Bearer <node token>`` of any configured site."""
    runtime = get_runtime(request)
    token = bearer_token(request.headers.get("authorization"))
    site_id = runtime.node_site_for_token(token)
    if site_id is None:
        logger.info("backplane request refused: %s", "no node token" if token is None else "wrong node token",
                    extra={"client": request.client.host if request.client else None})
        raise ApiProblem(401, "a node token is required: send Authorization: Bearer <node token>",
                         headers={"WWW-Authenticate": "Bearer"})
    document = runtime.backplane_document()  # NotSimulating -> 404
    state = _api_state(request)
    version = int(document["version"])
    effective = str(document["effective_ns"])
    if state.backplane is None or state.backplane[:2] != (version, effective):
        state.backplane = (version, effective, json.dumps(document, allow_nan=False).encode("utf-8"))
    logger.debug("backplane version %d served to site %s", version, site_id, extra={"site_id": site_id})
    return Response(state.backplane[2], media_type="application/json", headers={"Cache-Control": "no-store"})

