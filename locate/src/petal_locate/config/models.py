"""System configuration models (ARCHITECTURE.md section 3).

Pydantic v2 models with ``extra="forbid"``; YAML and JSON load to the same models
(``loader.py``). Config files use degrees for angles and name the unit in every field
(``lat_deg``, ``bandwidth_hz``, ``eirp_dbw``); frequencies are hertz, times of day are
ISO-8601 UTC strings, positions in ``table`` orbits are ECEF metres.

A validation failure is a ``pydantic.ValidationError`` (a ``ValueError``) whose entries
carry the path of the field at fault, including the cross-reference checks made on the
whole ``SystemConfig`` (unknown satellite ids, duplicate ids, a carrier outside its
transponder). ``field_errors`` turns one into ``[{"loc", "msg"}]`` with document paths;
``SystemConfig.warnings`` lists what is valid but worth a look, in the same shape.

Rules enforced here:

- Ids are 1 to 64 characters from ``A-Z a-z 0-9 . _ -`` starting with a letter or a
  digit (they end up in hash labels, URLs and file names). Satellite, site, carrier
  and channel ids are each unique across the whole configuration (channel ids across
  sites); transponder ids within a satellite; monitor ids within a channel.
- A ``Monitor`` reads ``nfft * navg`` samples at ``fs = span_hz``; it must satisfy
  ``span_hz <= max_sample_rate_hz`` of its channel and
  ``nfft * navg / span_hz <= 1 / frame_rate_hz``.
- A channel without monitors gets one derived window per transponder of each satellite
  it watches (``derive_monitors``).
- ``Region``: ``lon_min = -180`` with ``lon_max = 180`` is the full circle; otherwise
  the span runs eastward from ``lon_min`` to ``lon_max`` and wraps the antimeridian
  when ``lon_max < lon_min``.
"""
from __future__ import annotations

import datetime as _dt
import math
from collections.abc import Iterable, Sequence
from typing import Annotated, Any, Literal, Union

import numpy as np
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Discriminator,
    Field,
    PrivateAttr,
    Tag,
    field_validator,
    model_validator,
)
from pydantic_core import InitErrorDetails, PydanticCustomError, ValidationError

from ..core.antennas import electrically_small
from ..core.constants import WGS84_A
from ..core.geodesy import azel_to_ecef, look_angles, lla_to_ecef
from ..core.orbits import Orbit as NumericOrbit
from ..core.orbits import orbit_from_config, orbit_position, tle_checksum
from ..core.timebase import iso_from_ns, ns_from_datetime, ns_from_iso
from ..core.types import Uncertainty

__all__ = [
    "DEFAULT_MONITOR_FRAME_RATE_HZ",
    "DEFAULT_MONITOR_NAVG",
    "DEFAULT_MONITOR_NFFT",
    "MAX_MONITOR_NFFT",
    "MIN_DERIVED_NFFT",
    "MONITOR_SPAN_FACTOR",
    "Antenna",
    "AzElPointing",
    "BeamAntenna",
    "CafConfig",
    "CalibrationConfig",
    "CaptureConfig",
    "Carrier",
    "CarrierLocation",
    "ChannelPointing",
    "DetectionConfig",
    "EmitterModel",
    "EmitterPointing",
    "EphemerisSigma",
    "EphemerisUncertainty",
    "FixedPointing",
    "GeoOrbitModel",
    "IsotropicAntenna",
    "KeplerOrbitModel",
    "Monitor",
    "Orbit",
    "OrbitModel",
    "ParabolicAntenna",
    "Pointing",
    "ProcessingConfig",
    "ReceiveChannel",
    "Region",
    "Satellite",
    "Site",
    "SiteClock",
    "SolverConfig",
    "SystemConfig",
    "TableAntenna",
    "TableOrbitModel",
    "TableSample",
    "TleOrbitModel",
    "TrackPointing",
    "Transponder",
    "UncertaintyConfig",
    "derive_monitors",
    "field_errors",
    "fit_monitor_frame",
]

DEFAULT_MONITOR_NFFT: int = 1024
DEFAULT_MONITOR_NAVG: int = 8
DEFAULT_MONITOR_FRAME_RATE_HZ: float = 2.0
MIN_DERIVED_NFFT: int = 64
"""Smallest ``nfft`` a derived monitor is reduced to."""
MAX_MONITOR_NFFT: int = 131072
"""Largest ``nfft`` of a monitor (a PSD frame must fit one WebSocket message; PROTOCOL.md)."""
MONITOR_SPAN_FACTOR: float = 1.25
"""A derived monitor spans this many transponder bandwidths (when the channel can sample that fast)."""

_FIT_RTOL = 1e-12  # tolerance of the frame-fits-the-period comparison
_LON_TOL_DEG = 1e-9  # tolerance of Region longitude comparisons


# --------------------------------------------------------------------------- field types


def _iso_time(value: Any) -> Any:
    """Normalise a time given as ISO-8601 text or a YAML timestamp to ``...Z`` text."""
    if isinstance(value, _dt.datetime):
        return iso_from_ns(ns_from_datetime(value))
    if isinstance(value, _dt.date):
        return f"{value.year:04d}-{value.month:02d}-{value.day:02d}T00:00:00Z"
    if isinstance(value, str):
        try:
            return iso_from_ns(ns_from_iso(value))
        except ValueError:
            raise ValueError(
                "must be an ISO-8601 UTC time such as 2026-01-01T00:00:00Z"
            ) from None
    raise ValueError("must be an ISO-8601 UTC time such as 2026-01-01T00:00:00Z")


Ident = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")]
"""An id: 1 to 64 characters from ``A-Z a-z 0-9 . _ -``, starting with a letter or digit."""

MonitorId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,128}$")]
"""A monitor id: like ``Ident`` but up to 129 characters and allowing ``:`` (derived
monitors are named ``<satellite id>:<transponder id>``)."""

IsoTime = Annotated[str, BeforeValidator(_iso_time)]
"""UTC time as ISO-8601 text, normalised to ``YYYY-MM-DDTHH:MM:SS[.fff]Z``."""

PositiveFloat = Annotated[float, Field(gt=0)]
NonNegativeFloat = Annotated[float, Field(ge=0)]
LatDeg = Annotated[float, Field(ge=-90, le=90)]
LonDeg = Annotated[float, Field(ge=-180, le=180)]
Vector3 = Annotated[list[float], Field(min_length=3, max_length=3)]
Sigma3 = Annotated[list[NonNegativeFloat], Field(min_length=3, max_length=3)]


class _Model(BaseModel):
    """Base of every configuration model: unknown fields and non-finite numbers are errors."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


# --------------------------------------------------------------------------- orbits


def _require_buildable(model: Any) -> None:
    """Build the numeric orbit once so that anything it rejects is a validation error."""
    orbit_from_config(model)


class TleOrbitModel(_Model):
    """Two-line element set propagated with SGP4 (uses the ``tle`` ephemeris sigmas).

    line1, line2: the two lines, 69 characters each with valid checksums.
    """

    type: Literal["tle"]
    line1: str
    line2: str

    @field_validator("line1", "line2", mode="before")
    @classmethod
    def _strip(cls, value: Any) -> Any:
        return value.rstrip() if isinstance(value, str) else value

    @field_validator("line1", "line2")
    @classmethod
    def _check_line(cls, value: str, info: Any) -> str:
        number = "1" if info.field_name == "line1" else "2"
        if len(value) != 69:
            raise ValueError(f"a TLE line is 69 characters long, got {len(value)}")
        if value[0] != number:
            raise ValueError(f"{info.field_name} must start with {number!r}")
        expected = tle_checksum(value)
        if not value[68].isdigit() or int(value[68]) != expected:
            raise ValueError(f"TLE checksum mismatch: expected {expected}, found {value[68]!r}")
        return value

    @model_validator(mode="after")
    def _check_pair(self) -> TleOrbitModel:
        if self.line1[2:7] != self.line2[2:7]:
            raise ValueError("line2 carries a different catalogue number from line1")
        _require_buildable(self)
        return self


class GeoOrbitModel(_Model):
    """Analytic circular geosynchronous orbit for slot-style definitions (MATH.md section 2).

    lon_deg: longitude the ground track is centred on, degrees east in [-180, 180].
    inclination_deg: degrees. epoch: reference time. phase_deg: argument of latitude at
    the epoch, degrees. drift_deg_per_day: eastward drift, degrees per day.
    """

    type: Literal["geo"]
    lon_deg: LonDeg
    inclination_deg: float = Field(0.0, ge=0, le=180)
    epoch: IsoTime = "2026-01-01T00:00:00Z"
    phase_deg: float = 0.0
    drift_deg_per_day: float = 0.0

    @model_validator(mode="after")
    def _check(self) -> GeoOrbitModel:
        _require_buildable(self)
        return self


class KeplerOrbitModel(_Model):
    """Classical elements at an epoch, two-body with secular J2 (MATH.md section 2).

    a_m: semi-major axis, m. e: eccentricity. inc_deg, raan_deg, argp_deg,
    mean_anomaly_deg: degrees, inertial frame of ``gmst_rad``. epoch: ISO-8601 UTC.
    j2: apply the secular J2 rates.
    """

    type: Literal["kepler"]
    a_m: PositiveFloat
    e: float = Field(ge=0, lt=1)
    inc_deg: float = Field(ge=0, le=180)
    raan_deg: float
    argp_deg: float
    mean_anomaly_deg: float
    epoch: IsoTime
    j2: bool = True

    @model_validator(mode="after")
    def _check(self) -> KeplerOrbitModel:
        perigee = self.a_m * (1.0 - self.e)
        if perigee < WGS84_A:
            raise ValueError(
                f"a_m, e: the perigee radius a_m (1 - e) = {perigee:.0f} m is below the Earth's surface"
            )
        _require_buildable(self)
        return self


class TableSample(_Model):
    """One ephemeris sample: time, ECEF position (m) and ECEF velocity dr/dt (m/s)."""

    t: IsoTime
    r_m: Vector3
    v_mps: Vector3


class TableOrbitModel(_Model):
    """Operator ephemeris table in ECEF, cubic Hermite interpolated; never extrapolated."""

    type: Literal["table"]
    samples: list[TableSample] = Field(min_length=2)

    @model_validator(mode="after")
    def _check(self) -> TableOrbitModel:
        times = [ns_from_iso(sample.t) for sample in self.samples]
        for k in range(1, len(times)):
            if times[k] <= times[k - 1]:
                raise ValueError(
                    f"samples[{k}].t: sample times must be strictly increasing "
                    f"({self.samples[k].t} does not follow {self.samples[k - 1].t})"
                )
        _require_buildable(self)
        return self


OrbitModel = Annotated[
    Union[TleOrbitModel, GeoOrbitModel, KeplerOrbitModel, TableOrbitModel],
    Field(discriminator="type"),
]
"""The ``Orbit`` union of ARCHITECTURE.md section 3, discriminated on ``type``."""

Orbit = OrbitModel
"""Alias of ``OrbitModel`` under the name ARCHITECTURE.md uses (the configuration of an
orbit; the numeric orbit protocol is ``petal_locate.core.orbits.Orbit``)."""


# --------------------------------------------------------------------------- satellites


class Transponder(_Model):
    """One bent-pipe transponder.

    uplink_center_hz, bandwidth_hz: uplink passband, Hz. translation_hz: signed,
    downlink = uplink + translation. g_over_t_dbk: receive G/T, dB/K. sfd_dbw_m2:
    saturating flux density, dBW/m^2. eirp_sat_dbw: saturated EIRP, dBW.
    group_delay_s: transponder group delay, s.
    """

    id: Ident
    uplink_center_hz: PositiveFloat
    bandwidth_hz: PositiveFloat
    translation_hz: float
    g_over_t_dbk: float
    sfd_dbw_m2: float
    eirp_sat_dbw: float
    group_delay_s: NonNegativeFloat = 0.0

    @model_validator(mode="after")
    def _check(self) -> Transponder:
        if self.uplink_lo_hz <= 0.0:
            raise ValueError("bandwidth_hz: the uplink passband must lie above 0 Hz")
        if self.downlink_lo_hz <= 0.0:
            raise ValueError(
                "translation_hz: the downlink passband (uplink + translation) must lie above 0 Hz"
            )
        return self

    @property
    def uplink_lo_hz(self) -> float:
        """Lower edge of the uplink passband, Hz."""
        return self.uplink_center_hz - 0.5 * self.bandwidth_hz

    @property
    def uplink_hi_hz(self) -> float:
        """Upper edge of the uplink passband, Hz."""
        return self.uplink_center_hz + 0.5 * self.bandwidth_hz

    @property
    def downlink_center_hz(self) -> float:
        """Centre of the downlink passband, Hz."""
        return self.uplink_center_hz + self.translation_hz

    @property
    def downlink_lo_hz(self) -> float:
        """Lower edge of the downlink passband, Hz."""
        return self.downlink_center_hz - 0.5 * self.bandwidth_hz

    @property
    def downlink_hi_hz(self) -> float:
        """Upper edge of the downlink passband, Hz."""
        return self.downlink_center_hz + 0.5 * self.bandwidth_hz

    def relays(self, f_up_hz: float) -> bool:
        """Whether an emitter centred on ``f_up_hz`` (Hz) is relayed: the centre lies in
        the closed uplink passband (MATH.md section 4)."""
        return self.uplink_lo_hz <= f_up_hz <= self.uplink_hi_hz


class EphemerisSigma(_Model):
    """One-sigma ephemeris uncertainty in the radial / in-track / cross-track frame.

    pos_m: position sigmas, m. vel_mps: velocity sigmas, m/s.
    """

    pos_m: Sigma3
    vel_mps: Sigma3


class Satellite(_Model):
    """A satellite: its orbit and its transponders.

    ephemeris_sigma: RIC one-sigma override; otherwise the sigmas follow the orbit type
    (``tle`` -> ``uncertainty.ephemeris.tle``, everything else -> ``operator``).
    """

    id: Ident
    name: str
    norad_id: int | None = Field(None, ge=0)
    notes: str = ""
    orbit: OrbitModel
    transponders: list[Transponder]
    ephemeris_sigma: EphemerisSigma | None = None

    def transponder(self, transponder_id: str) -> Transponder:
        """The transponder with this id; ValueError naming it when there is none."""
        for transponder in self.transponders:
            if transponder.id == transponder_id:
                return transponder
        raise ValueError(
            f"transponder_id {transponder_id!r} is not a transponder of satellite {self.id!r}"
        )


# --------------------------------------------------------------------------- antennas


class IsotropicAntenna(_Model):
    """The same gain (dBi) in every direction."""

    type: Literal["isotropic"] = "isotropic"
    gain_dbi: float = 0.0

    def describe(self) -> str:
        """Short human-readable summary."""
        return f"isotropic {self.gain_dbi:g} dBi"


class ParabolicAntenna(_Model):
    """A reflector of ``diameter_m`` metres with aperture ``efficiency`` (a PETAL dish).

    Below 3 wavelengths across it is electrically small (``core.antennas``).
    """

    type: Literal["parabolic"]
    diameter_m: PositiveFloat
    efficiency: float = Field(0.55, gt=0, le=1)

    def describe(self) -> str:
        """Short human-readable summary."""
        return f"{self.diameter_m:g} m parabolic (efficiency {self.efficiency:g})"


class BeamAntenna(_Model):
    """A single-beam antenna (yagi, helix, patch): boresight gain (dBi), half-power
    beamwidth (degrees) and front-to-back ratio (dB)."""

    type: Literal["beam"]
    gain_dbi: float
    hpbw_deg: float = Field(gt=0, le=360)
    front_to_back_db: float = Field(20.0, ge=0)

    def describe(self) -> str:
        """Short human-readable summary."""
        return f"{self.gain_dbi:g} dBi beam (HPBW {self.hpbw_deg:g} deg)"


class TableAntenna(_Model):
    """A tabulated axisymmetric pattern: gain (dBi) against off-axis angle (degrees,
    strictly increasing within [0, 180]); linear interpolation in dB, ends held."""

    type: Literal["table"]
    theta_deg: list[float] = Field(min_length=1)
    gain_dbi: list[float] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> TableAntenna:
        if len(self.gain_dbi) != len(self.theta_deg):
            raise ValueError(
                f"gain_dbi: needs one value per theta_deg entry ({len(self.theta_deg)}), "
                f"got {len(self.gain_dbi)}"
            )
        theta = self.theta_deg
        if theta[0] < 0.0 or theta[-1] > 180.0 or any(b <= a for a, b in zip(theta, theta[1:])):
            raise ValueError("theta_deg: must be strictly increasing within [0, 180] degrees")
        return self

    def describe(self) -> str:
        """Short human-readable summary."""
        return f"tabulated pattern, peak {max(self.gain_dbi):g} dBi ({len(self.theta_deg)} points)"


Antenna = Annotated[
    Union[IsotropicAntenna, ParabolicAntenna, BeamAntenna, TableAntenna],
    Field(discriminator="type"),
]
"""The ``Antenna`` union of ARCHITECTURE.md section 3, discriminated on ``type``."""


# --------------------------------------------------------------------------- pointing


class TrackPointing(_Model):
    """Boresight follows a satellite (recomputed from its orbit)."""

    satellite_id: Ident


class FixedPointing(_Model):
    """Fixed boresight of a receive channel; it watches the listed satellites.

    az_deg: azimuth clockwise from true north, degrees. el_deg: elevation above the
    local horizontal, degrees. satellite_ids: at least one satellite id.
    """

    az_deg: float = Field(ge=-360, le=360)
    el_deg: float = Field(ge=-90, le=90)
    satellite_ids: list[Ident] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> FixedPointing:
        if len(set(self.satellite_ids)) != len(self.satellite_ids):
            raise ValueError("satellite_ids: a satellite is listed more than once")
        return self


class AzElPointing(_Model):
    """Fixed boresight of an emitter: azimuth clockwise from true north and elevation
    above the local horizontal, degrees."""

    az_deg: float = Field(ge=-360, le=360)
    el_deg: float = Field(ge=-90, le=90)


def _channel_pointing_tag(value: Any) -> str | None:
    if isinstance(value, TrackPointing):
        return "track"
    if isinstance(value, FixedPointing):
        return "fixed"
    if isinstance(value, dict):
        return "track" if "satellite_id" in value else "fixed"
    return None


def _emitter_pointing_tag(value: Any) -> str | None:
    if isinstance(value, str):
        return "omni"
    if isinstance(value, TrackPointing):
        return "track"
    if isinstance(value, AzElPointing):
        return "azel"
    if isinstance(value, dict):
        return "track" if "satellite_id" in value else "azel"
    return None


ChannelPointing = Annotated[
    Union[Annotated[TrackPointing, Tag("track")], Annotated[FixedPointing, Tag("fixed")]],
    Discriminator(
        _channel_pointing_tag,
        custom_error_type="pointing_form",
        custom_error_message="must be {satellite_id} or {az_deg, el_deg, satellite_ids}",
    ),
]
"""``{satellite_id}`` (tracks it) or ``{az_deg, el_deg, satellite_ids}`` (fixed boresight)."""

Pointing = ChannelPointing
"""Alias of ``ChannelPointing`` under the name ARCHITECTURE.md uses."""

EmitterPointing = Annotated[
    Union[
        Annotated[Literal["omni"], Tag("omni")],
        Annotated[TrackPointing, Tag("track")],
        Annotated[AzElPointing, Tag("azel")],
    ],
    Discriminator(
        _emitter_pointing_tag,
        custom_error_type="pointing_form",
        custom_error_message='must be "omni", {satellite_id} or {az_deg, el_deg}',
    ),
]
"""``"omni"``, ``{satellite_id}`` (aimed at it) or ``{az_deg, el_deg}`` (fixed boresight)."""


# --------------------------------------------------------------------------- sites


class Monitor(_Model):
    """One spectrum window of a receive channel (downlink frequencies).

    The node reads ``nfft * navg`` samples at ``fs = span_hz`` centred on ``center_hz``
    every ``1 / frame_rate_hz`` seconds, so the frame must fit the frame period.
    """

    id: MonitorId
    center_hz: PositiveFloat
    span_hz: PositiveFloat
    nfft: int = Field(DEFAULT_MONITOR_NFFT, ge=2, le=MAX_MONITOR_NFFT)
    navg: int = Field(DEFAULT_MONITOR_NAVG, ge=1)
    frame_rate_hz: PositiveFloat = DEFAULT_MONITOR_FRAME_RATE_HZ

    @model_validator(mode="after")
    def _check(self) -> Monitor:
        if self.center_hz - 0.5 * self.span_hz <= 0.0:
            raise ValueError(f"monitor {self.id!r}: span_hz reaches below 0 Hz")
        if not _frame_fits(self.nfft, self.navg, self.span_hz, self.frame_rate_hz):
            raise ValueError(
                f"monitor {self.id!r}: a frame of nfft * navg = {self.nfft * self.navg} samples at "
                f"span_hz = {self.span_hz:g} Hz lasts {self.frame_duration_s:g} s, longer than the "
                f"frame period 1 / frame_rate_hz = {1.0 / self.frame_rate_hz:g} s"
            )
        return self

    @property
    def frame_duration_s(self) -> float:
        """Duration of the samples behind one frame, ``nfft * navg / span_hz``, s."""
        return self.nfft * self.navg / self.span_hz

    @property
    def bin_hz(self) -> float:
        """Width of one PSD bin, ``span_hz / nfft``, Hz."""
        return self.span_hz / self.nfft


class ReceiveChannel(_Model):
    """One receive chain of a site: antenna, pointing, noise and its spectrum monitors.

    system_noise_temp_k: system noise temperature, K. polarization_loss_db: loss applied
    to everything this channel receives, dB. max_sample_rate_hz: highest complex sample
    rate, Hz. monitors: explicit spectrum windows; when empty, ``SystemConfig`` derives
    one per transponder of each watched satellite.
    """

    id: Ident
    name: str = ""
    antenna: Antenna
    pointing: ChannelPointing
    system_noise_temp_k: PositiveFloat = 400.0
    polarization_loss_db: NonNegativeFloat = 0.0
    max_sample_rate_hz: PositiveFloat = 2e6
    monitors: list[Monitor] = Field(default_factory=list)

    _monitors_derived: bool = PrivateAttr(default=False)

    @model_validator(mode="after")
    def _check(self) -> ReceiveChannel:
        seen: set[str] = set()
        for monitor in self.monitors:
            if monitor.id in seen:
                raise ValueError(f"monitors: monitor id {monitor.id!r} is used more than once in this channel")
            seen.add(monitor.id)
            if monitor.span_hz > self.max_sample_rate_hz * (1.0 + _FIT_RTOL):
                raise ValueError(
                    f"monitor {monitor.id!r}: span_hz = {monitor.span_hz:g} Hz exceeds the channel's "
                    f"max_sample_rate_hz = {self.max_sample_rate_hz:g} Hz"
                )
        return self

    @property
    def watched_satellite_ids(self) -> tuple[str, ...]:
        """Ids of the satellites this channel watches (the tracked one, or the listed ones)."""
        if isinstance(self.pointing, TrackPointing):
            return (self.pointing.satellite_id,)
        return tuple(self.pointing.satellite_ids)

    @property
    def monitors_derived(self) -> bool:
        """True when ``monitors`` was filled in by ``SystemConfig`` rather than configured."""
        return self._monitors_derived

    @property
    def antenna_summary(self) -> str:
        """Human-readable description of the antenna (for assignments and logs)."""
        return self.antenna.describe()


class SiteClock(_Model):
    """Site time reference. sigma_ns: one-sigma clock offset, ns."""

    sigma_ns: NonNegativeFloat = 30.0


class Site(_Model):
    """A ground site. The antenna phase centre is at ``alt_m + antenna_height_m`` above
    the WGS84 ellipsoid (``xyz()``); ``antenna_height_m`` (above ground) also sets the
    radio horizon of direct paths."""

    id: Ident
    name: str
    lat_deg: LatDeg
    lon_deg: LonDeg
    alt_m: float = 0.0
    antenna_height_m: NonNegativeFloat = 10.0
    clock: SiteClock = Field(default_factory=SiteClock)
    channels: list[ReceiveChannel]

    def xyz(self) -> np.ndarray:
        """Antenna phase centre, ECEF m, ``ndarray[3]``."""
        return lla_to_ecef(self.lat_deg, self.lon_deg, self.alt_m + self.antenna_height_m)


# --------------------------------------------------------------------------- emitters and carriers


class EmitterModel(_Model):
    """What an emitter radiates, independent of any scenario (MATH.md section 10).

    eirp_dbw: EIRP on boresight, dBW. antenna, pointing: the pattern and where it
    points (``"omni"`` applies no pattern). bandwidth_hz: occupied bandwidth, Hz
    (0 only for ``cw``). waveform: ``cw``, ``noise``, ``chirp`` or ``psk``.
    """

    eirp_dbw: float
    antenna: Antenna = Field(default_factory=IsotropicAntenna)
    pointing: EmitterPointing = "omni"
    bandwidth_hz: NonNegativeFloat
    waveform: Literal["cw", "noise", "chirp", "psk"] = "noise"

    @model_validator(mode="after")
    def _check(self) -> EmitterModel:
        if self.bandwidth_hz <= 0.0 and self.waveform != "cw":
            raise ValueError(f"bandwidth_hz: must be positive for a {self.waveform!r} waveform")
        return self


class CarrierLocation(_Model):
    """Surveyed position of a carrier's uplink antenna (phase centre at
    ``alt_m + antenna_height_m`` above the ellipsoid)."""

    lat_deg: LatDeg
    lon_deg: LonDeg
    alt_m: float = 0.0
    antenna_height_m: NonNegativeFloat = 10.0

    def xyz(self) -> np.ndarray:
        """Antenna phase centre, ECEF m, ``ndarray[3]``."""
        return lla_to_ecef(self.lat_deg, self.lon_deg, self.alt_m + self.antenna_height_m)


class Carrier(_Model):
    """An authorised carrier of the frequency plan.

    satellite_id, transponder_id: the transponder it is assigned to.
    uplink_center_hz, bandwidth_hz: its uplink allocation, Hz. location, emitter: where
    and what it radiates, when known. use_as_reference: usable as a calibration
    reference (requires location and emitter).
    """

    id: Ident
    name: str
    satellite_id: Ident
    transponder_id: Ident
    uplink_center_hz: PositiveFloat
    bandwidth_hz: PositiveFloat
    location: CarrierLocation | None = None
    emitter: EmitterModel | None = None
    use_as_reference: bool = False

    @model_validator(mode="after")
    def _check(self) -> Carrier:
        if self.use_as_reference and (self.location is None or self.emitter is None):
            missing = " and ".join(
                name for name, value in (("location", self.location), ("emitter", self.emitter)) if value is None
            )
            raise ValueError(f"use_as_reference: a reference carrier needs {missing}")
        if self.emitter is not None and self.emitter.bandwidth_hz > self.bandwidth_hz * (1.0 + _FIT_RTOL):
            raise ValueError(
                f"emitter.bandwidth_hz: {self.emitter.bandwidth_hz:g} Hz is wider than the carrier's "
                f"bandwidth_hz = {self.bandwidth_hz:g} Hz"
            )
        return self

    @property
    def uplink_lo_hz(self) -> float:
        """Lower edge of the uplink allocation, Hz."""
        return self.uplink_center_hz - 0.5 * self.bandwidth_hz

    @property
    def uplink_hi_hz(self) -> float:
        """Upper edge of the uplink allocation, Hz."""
        return self.uplink_center_hz + 0.5 * self.bandwidth_hz


# --------------------------------------------------------------------------- processing


class DetectionConfig(_Model):
    """Spectrum detection and uplink/local verdict settings (MATH.md section 7)."""

    threshold_db: PositiveFloat = 6.0
    confirm_m: int = Field(3, ge=1)
    confirm_n: int = Field(5, ge=1)
    release_frames: int = Field(6, ge=1)
    merge_bins: int = Field(2, ge=0)
    baseline_tau_s: PositiveFloat = 600.0
    power_anomaly_db: NonNegativeFloat = 6.0
    min_site_separation_km: NonNegativeFloat = 200.0
    verdict_settle_s: NonNegativeFloat = 5.0
    sensitivity_margin_db: NonNegativeFloat = 6.0

    @model_validator(mode="after")
    def _check(self) -> DetectionConfig:
        if self.confirm_m > self.confirm_n:
            raise ValueError(
                f"confirm_m: {self.confirm_m} detections cannot be seen in confirm_n = {self.confirm_n} frames"
            )
        return self


class CaptureConfig(_Model):
    """IQ capture tasking (ARCHITECTURE.md section 7). Durations in s, rates in Hz."""

    duration_s: PositiveFloat = 1.0
    lead_s: NonNegativeFloat = 0.6
    interval_s: PositiveFloat = 5.0
    oversample: float = Field(1.25, ge=1)
    min_sample_rate_hz: PositiveFloat = 20e3
    max_samples: int = Field(262144, ge=2)
    timeout_s: PositiveFloat = 15.0


class CafConfig(_Model):
    """Cross-ambiguity estimation settings (MATH.md section 6)."""

    min_snr_db: float = 13.0
    max_tdoa_sigma_s: PositiveFloat = 5e-4
    doppler_margin_hz: NonNegativeFloat = 20.0
    delay_margin_s: NonNegativeFloat = 20e-6


class SolverConfig(_Model):
    """Position solver settings (MATH.md section 9)."""

    height_m: float = 0.0
    min_elevation_deg: float = Field(0.0, ge=0, lt=90)
    confidence: float = Field(0.95, gt=0, lt=1)
    grid_step_deg: float = Field(1.0, gt=0, le=45)
    n_starts: int = Field(16, ge=1)
    chi2_ambiguity: NonNegativeFloat = 9.0
    max_measurements: int = Field(400, ge=2)


def _operator_sigma() -> EphemerisSigma:
    return EphemerisSigma(pos_m=[100.0, 1000.0, 1000.0], vel_mps=[0.02, 0.02, 0.05])


def _tle_sigma() -> EphemerisSigma:
    return EphemerisSigma(pos_m=[2000.0, 20000.0, 10000.0], vel_mps=[0.3, 0.3, 0.7])


class EphemerisUncertainty(_Model):
    """Default RIC one-sigma ephemeris errors by orbit source: ``operator`` for ``geo``,
    ``kepler`` and ``table`` orbits, ``tle`` for ``tle`` orbits."""

    operator: EphemerisSigma = Field(default_factory=_operator_sigma)
    tle: EphemerisSigma = Field(default_factory=_tle_sigma)


class UncertaintyConfig(_Model):
    """Systematic (consider) uncertainties, all one-sigma.

    sat_osc_frac: fractional error of a satellite's translation oscillator,
    uncalibrated. site_osc_frac: fractional error of a site's reference oscillator.
    tdoa_bias_ns: per-pair TDOA bias, ns.
    """

    ephemeris: EphemerisUncertainty = Field(default_factory=EphemerisUncertainty)
    sat_osc_frac: NonNegativeFloat = 1e-6
    site_osc_frac: NonNegativeFloat = 1e-11
    tdoa_bias_ns: NonNegativeFloat = 50.0


class CalibrationConfig(_Model):
    """Reference-emitter calibration schedule. interval_s, max_age_s: seconds."""

    enabled: bool = True
    interval_s: PositiveFloat = 30.0
    max_age_s: PositiveFloat = 300.0


class Region(_Model):
    """Latitude/longitude box of admissible emitter positions, degrees.

    ``lon_min = -180`` with ``lon_max = 180`` is the full circle. Otherwise the box
    runs eastward from ``lon_min`` to ``lon_max``; ``lon_max < lon_min`` wraps the
    antimeridian, and equal values (or -180 against 180 the other way round) are
    invalid.
    """

    lat_min: LatDeg = -70.0
    lat_max: LatDeg = 70.0
    lon_min: LonDeg = -180.0
    lon_max: LonDeg = 180.0

    @model_validator(mode="after")
    def _check(self) -> Region:
        if not self.lat_min < self.lat_max:
            raise ValueError("lat_max: must be greater than lat_min")
        if not self.is_full_circle and math.fmod(self.lon_max - self.lon_min, 360.0) == 0.0:
            raise ValueError(
                "lon_max: lon_min and lon_max are the same meridian "
                "(use lon_min = -180, lon_max = 180 for the full circle)"
            )
        return self

    @property
    def is_full_circle(self) -> bool:
        """True when the region covers every longitude."""
        return self.lon_min == -180.0 and self.lon_max == 180.0

    def lon_span_deg(self) -> float:
        """Eastward extent in longitude, degrees: 360 for the full circle, otherwise in (0, 360)."""
        if self.is_full_circle:
            return 360.0
        return float((self.lon_max - self.lon_min) % 360.0)

    def contains(self, lat_deg: Any, lon_deg: Any) -> Any:
        """Whether point(s) lie inside the region (edges included).

        lat_deg, lon_deg: degrees, broadcast against each other; longitudes may be
        given in any 360 degree range (wrapped or unwrapped). Returns a bool array of
        the broadcast shape (a numpy bool for scalars).
        """
        lat = np.asarray(lat_deg, dtype=np.float64)
        lon = np.asarray(lon_deg, dtype=np.float64)
        inside = (lat >= self.lat_min) & (lat <= self.lat_max)
        if not self.is_full_circle:
            east = np.mod(lon - self.lon_min, 360.0)  # degrees east of lon_min, [0, 360)
            inside = inside & ((east <= self.lon_span_deg() + _LON_TOL_DEG) | (east >= 360.0 - _LON_TOL_DEG))
        else:
            inside = inside & np.isfinite(lon)
        return inside[()] if inside.ndim == 0 else inside

    def grid(self, step_deg: float) -> tuple[np.ndarray, np.ndarray]:
        """Regular grid over the region: ``(lat_deg[n_lat], lon_deg[n_lon])``.

        ``lat_deg = lat_min + k * step`` up to ``lat_max``; ``lon_deg = lon_min + k * step``
        up to ``lon_min + lon_span_deg()``, increasing and **unwrapped** (values exceed
        180 for a region that crosses the antimeridian). The full circle stops one
        step short of ``lon_min + 360`` so that no meridian appears twice.
        """
        step = float(step_deg)
        if not (math.isfinite(step) and step > 0.0):
            raise ValueError(f"step_deg must be positive, got {step_deg!r}")
        n_lat = int(math.floor((self.lat_max - self.lat_min) / step + 1e-9)) + 1
        span = self.lon_span_deg()
        if self.is_full_circle:
            n_lon = max(int(math.ceil(span / step - 1e-9)), 1)
        else:
            n_lon = int(math.floor(span / step + 1e-9)) + 1
        lat = np.minimum(self.lat_min + step * np.arange(n_lat), self.lat_max)
        lon = np.minimum(self.lon_min + step * np.arange(n_lon), self.lon_min + span)
        return lat, lon


class ProcessingConfig(_Model):
    """All processing settings; every section has defaults."""

    detection: DetectionConfig = Field(default_factory=DetectionConfig)
    capture: CaptureConfig = Field(default_factory=CaptureConfig)
    caf: CafConfig = Field(default_factory=CafConfig)
    solver: SolverConfig = Field(default_factory=SolverConfig)
    uncertainty: UncertaintyConfig = Field(default_factory=UncertaintyConfig)
    calibration: CalibrationConfig = Field(default_factory=CalibrationConfig)
    region: Region = Field(default_factory=Region)


# --------------------------------------------------------------------------- monitor derivation


def _frame_fits(nfft: int, navg: int, span_hz: float, frame_rate_hz: float) -> bool:
    """``nfft * navg / span_hz <= 1 / frame_rate_hz`` (with a rounding tolerance)."""
    return nfft * navg * frame_rate_hz <= span_hz * (1.0 + _FIT_RTOL)


def fit_monitor_frame(
    span_hz: float,
    frame_rate_hz: float = DEFAULT_MONITOR_FRAME_RATE_HZ,
    nfft: int = DEFAULT_MONITOR_NFFT,
    navg: int = DEFAULT_MONITOR_NAVG,
) -> tuple[int, int]:
    """Reduce ``(nfft, navg)`` until a frame fits the frame period.

    span_hz: window width and sample rate, Hz. frame_rate_hz: frames per second.
    ``navg`` is reduced (minimum 1) until ``nfft * navg / span_hz <= 1 / frame_rate_hz``;
    when even ``navg = 1`` does not fit, ``nfft`` is halved (minimum 64) until it does.
    Returns ``(nfft, navg)``; raises ValueError when ``nfft = 64, navg = 1`` still does
    not fit (the window is too narrow for the frame rate).
    """
    if not (span_hz > 0.0 and frame_rate_hz > 0.0):
        raise ValueError("span_hz and frame_rate_hz must be positive")
    if nfft < MIN_DERIVED_NFFT or navg < 1:
        raise ValueError(f"nfft must be at least {MIN_DERIVED_NFFT} and navg at least 1")
    budget = span_hz * (1.0 + _FIT_RTOL) / frame_rate_hz  # samples that fit one frame period
    navg = max(1, min(navg, int(budget // nfft)))
    while nfft * navg > budget and nfft // 2 >= MIN_DERIVED_NFFT:
        nfft //= 2
    if nfft * navg > budget:
        raise ValueError(
            f"span_hz = {span_hz:g} Hz is too narrow for frame_rate_hz = {frame_rate_hz:g}: "
            f"even nfft = {MIN_DERIVED_NFFT}, navg = 1 does not fit the frame period"
        )
    return nfft, navg


def derive_monitors(channel: ReceiveChannel, satellites: Iterable[Satellite]) -> list[Monitor]:
    """Default spectrum windows of a channel: one per transponder of each watched satellite.

    ``center_hz`` is the transponder's downlink centre and
    ``span_hz = min(1.25 * bandwidth_hz, max_sample_rate_hz)`` (so a transponder wider
    than the channel can sample is monitored over its central ``max_sample_rate_hz``
    only); ``nfft`` and ``navg`` follow ``fit_monitor_frame`` at 2 frames per second.
    Monitor ids are ``<satellite id>:<transponder id>``. Watched satellites that share
    a downlink plan would give identical windows; each distinct ``(center_hz, span_hz)``
    is monitored once, under the first satellite that has it.

    satellites: the satellites of the configuration (those the channel does not watch
    are ignored; a watched id that is missing raises ValueError).
    """
    by_id = {satellite.id: satellite for satellite in satellites}
    monitors: list[Monitor] = []
    seen: set[tuple[float, float]] = set()
    for sat_id in channel.watched_satellite_ids:
        if sat_id not in by_id:
            raise ValueError(f"pointing: satellite id {sat_id!r} is not in the configuration")
        for transponder in by_id[sat_id].transponders:
            span = min(MONITOR_SPAN_FACTOR * transponder.bandwidth_hz, channel.max_sample_rate_hz)
            key = (transponder.downlink_center_hz, span)
            if key in seen:
                continue
            seen.add(key)
            try:
                nfft, navg = fit_monitor_frame(span)
            except ValueError as exc:
                raise ValueError(
                    f"monitors: cannot derive a window for transponder {transponder.id!r} of "
                    f"satellite {sat_id!r}: {exc}"
                ) from None
            monitors.append(
                Monitor(
                    id=f"{sat_id}:{transponder.id}",
                    center_hz=transponder.downlink_center_hz,
                    span_hz=span,
                    nfft=nfft,
                    navg=navg,
                    frame_rate_hz=DEFAULT_MONITOR_FRAME_RATE_HZ,
                )
            )
    return monitors


# --------------------------------------------------------------------------- the system


_Loc = tuple[Union[str, int], ...]


class _Problems:
    """Collects cross-reference errors with the path of the field at fault."""

    def __init__(self) -> None:
        self.items: list[InitErrorDetails] = []

    def add(self, loc: _Loc, message: str, value: Any = None) -> None:
        self.items.append(
            InitErrorDetails(
                type=PydanticCustomError("config_error", "{reason}", {"reason": message}),
                loc=loc,
                input=value,
            )
        )

    def raise_if_any(self) -> None:
        if self.items:
            raise ValidationError.from_exception_data("SystemConfig", self.items)


class SystemConfig(_Model):
    """A complete system: satellites, sites, the frequency plan and processing settings.

    version: revision of this configuration (the server's ETag). dut1_s: UT1 - UTC, s.
    """

    version: int = Field(1, ge=0)
    name: str = Field(min_length=1)
    description: str = ""
    dut1_s: float = Field(0.0, ge=-1, le=1)
    satellites: list[Satellite]
    sites: list[Site]
    carriers: list[Carrier] = Field(default_factory=list)
    processing: ProcessingConfig = Field(default_factory=ProcessingConfig)

    # ------------------------------------------------------------------ validation

    @model_validator(mode="after")
    def _cross_check(self) -> SystemConfig:
        problems = _Problems()
        self._check_unique_ids(problems)
        sats = {satellite.id: satellite for satellite in self.satellites}
        for s, site in enumerate(self.sites):
            for c, channel in enumerate(site.channels):
                loc: _Loc = ("sites", s, "channels", c, "pointing")
                if isinstance(channel.pointing, TrackPointing):
                    if channel.pointing.satellite_id not in sats:
                        problems.add(
                            loc + ("satellite_id",),
                            f"unknown satellite id {channel.pointing.satellite_id!r}",
                            channel.pointing.satellite_id,
                        )
                else:
                    for k, sat_id in enumerate(channel.pointing.satellite_ids):
                        if sat_id not in sats:
                            problems.add(loc + ("satellite_ids", k), f"unknown satellite id {sat_id!r}", sat_id)
        for k, carrier in enumerate(self.carriers):
            self._check_carrier(k, carrier, sats, problems)
        problems.raise_if_any()

        # Derive the monitors of channels that configure none. Sites and channels handed
        # in as model instances may belong to another configuration too, so nothing is
        # changed in place: a channel that gets monitors is replaced by a copy.
        sites: list[Site] = []
        for s, site in enumerate(self.sites):
            channels: list[ReceiveChannel] = []
            for c, channel in enumerate(site.channels):
                if channel.monitors and not channel._monitors_derived:
                    channels.append(channel)  # explicit monitors are kept as configured
                    continue
                try:
                    derived = derive_monitors(channel, self.satellites)
                except ValueError as exc:
                    problems.add(("sites", s, "channels", c, "monitors"), str(exc))
                    continue
                fresh = channel.model_copy(update={"monitors": derived})
                fresh._monitors_derived = True
                channels.append(fresh)
            sites.append(site.model_copy(update={"channels": channels}))
        problems.raise_if_any()
        self.sites = sites
        return self

    def _check_unique_ids(self, problems: _Problems) -> None:
        def unique(entries: Iterable[tuple[_Loc, str]], what: str) -> None:
            first: dict[str, _Loc] = {}
            for loc, ident in entries:
                if ident in first:
                    problems.add(
                        loc + ("id",),
                        f"{what} id {ident!r} is already used at {_dotted(first[ident])}",
                        ident,
                    )
                else:
                    first[ident] = loc

        unique(((("satellites", k), sat.id) for k, sat in enumerate(self.satellites)), "satellite")
        unique(((("sites", k), site.id) for k, site in enumerate(self.sites)), "site")
        unique(((("carriers", k), carrier.id) for k, carrier in enumerate(self.carriers)), "carrier")
        unique(
            (
                (("sites", s, "channels", c), channel.id)
                for s, site in enumerate(self.sites)
                for c, channel in enumerate(site.channels)
            ),
            "channel",
        )
        for k, sat in enumerate(self.satellites):
            unique(
                ((("satellites", k, "transponders", t), tp.id) for t, tp in enumerate(sat.transponders)),
                f"transponder (of satellite {sat.id!r})",
            )

    @staticmethod
    def _check_carrier(k: int, carrier: Carrier, sats: dict[str, Satellite], problems: _Problems) -> None:
        loc: _Loc = ("carriers", k)
        satellite = sats.get(carrier.satellite_id)
        if satellite is None:
            problems.add(
                loc + ("satellite_id",), f"unknown satellite id {carrier.satellite_id!r}", carrier.satellite_id
            )
        else:
            transponder = next((tp for tp in satellite.transponders if tp.id == carrier.transponder_id), None)
            if transponder is None:
                problems.add(
                    loc + ("transponder_id",),
                    f"unknown transponder id {carrier.transponder_id!r} on satellite {satellite.id!r}",
                    carrier.transponder_id,
                )
            elif not transponder.relays(carrier.uplink_center_hz):
                problems.add(
                    loc + ("uplink_center_hz",),
                    f"{carrier.uplink_center_hz:.0f} Hz is outside the uplink passband of transponder "
                    f"{transponder.id!r} ({transponder.uplink_lo_hz:.0f} to {transponder.uplink_hi_hz:.0f} Hz)",
                    carrier.uplink_center_hz,
                )
        if carrier.emitter is not None and isinstance(carrier.emitter.pointing, TrackPointing):
            target = carrier.emitter.pointing.satellite_id
            if target not in sats:
                problems.add(loc + ("emitter", "pointing", "satellite_id"), f"unknown satellite id {target!r}", target)

    # ------------------------------------------------------------------ lookups

    def satellite(self, sat_id: str) -> Satellite:
        """The satellite with this id; ValueError naming it when there is none."""
        for satellite in self.satellites:
            if satellite.id == sat_id:
                return satellite
        raise ValueError(f"sat_id {sat_id!r} is not a satellite of configuration {self.name!r}")

    def site(self, site_id: str) -> Site:
        """The site with this id; ValueError naming it when there is none."""
        for site in self.sites:
            if site.id == site_id:
                return site
        raise ValueError(f"site_id {site_id!r} is not a site of configuration {self.name!r}")

    def channel(self, channel_id: str) -> tuple[Site, ReceiveChannel]:
        """The receive channel with this id and the site it belongs to."""
        for site in self.sites:
            for channel in site.channels:
                if channel.id == channel_id:
                    return site, channel
        raise ValueError(f"channel_id {channel_id!r} is not a channel of configuration {self.name!r}")

    def carrier(self, carrier_id: str) -> Carrier:
        """The carrier with this id; ValueError naming it when there is none."""
        for carrier in self.carriers:
            if carrier.id == carrier_id:
                return carrier
        raise ValueError(f"carrier_id {carrier_id!r} is not a carrier of configuration {self.name!r}")

    def reference_carriers(self) -> list[Carrier]:
        """Carriers with ``use_as_reference`` set (each has a location and an emitter)."""
        return [carrier for carrier in self.carriers if carrier.use_as_reference]

    def carrier_xyz(self, carrier_id: str) -> np.ndarray:
        """Antenna phase centre of a carrier with a ``location``, ECEF m, ``ndarray[3]``."""
        carrier = self.carrier(carrier_id)
        if carrier.location is None:
            raise ValueError(f"carrier_id {carrier_id!r} has no location")
        return carrier.location.xyz()

    # ------------------------------------------------------------------ geometry

    def site_xyz(self, site_id: str) -> np.ndarray:
        """Antenna phase centre of a site, ECEF m, ``ndarray[3]``:
        ``lla_to_ecef(lat_deg, lon_deg, alt_m + antenna_height_m)``."""
        return self.site(site_id).xyz()

    def sites_xyz(self) -> dict[str, np.ndarray]:
        """Antenna phase centre of every site: ``dict[site_id, ndarray[3]]`` (ECEF m)."""
        return {site.id: site.xyz() for site in self.sites}

    def orbit(self, sat_id: str) -> NumericOrbit:
        """The numeric orbit of one satellite (``core.orbits``), built with ``dut1_s``."""
        return orbit_from_config(self.satellite(sat_id).orbit, self.dut1_s)

    def orbits(self) -> dict[str, NumericOrbit]:
        """The numeric orbit of every satellite: ``dict[sat_id, Orbit]``.

        Built from the configuration on every call; hold on to the result.
        """
        return {
            satellite.id: orbit_from_config(satellite.orbit, self.dut1_s) for satellite in self.satellites
        }

    def boresight_ecef(self, channel_id: str, t_s: Any) -> np.ndarray:
        """Unit ECEF vector along the boresight of a channel's antenna at Unix seconds ``t_s``.

        A tracking channel points from the site's phase centre at the geometric
        position of its satellite at ``t_s``; a fixed channel points along its
        configured azimuth and elevation. ``t_s`` scalar gives ``ndarray[3]``, an array
        ``[M]`` gives ``[M, 3]``.
        """
        site, channel = self.channel(channel_id)
        t = np.asarray(t_s, dtype=np.float64)
        if not np.all(np.isfinite(t)):
            raise ValueError("t_s must be finite")
        if isinstance(channel.pointing, TrackPointing):
            los = orbit_position(self.orbit(channel.pointing.satellite_id), t) - site.xyz()
            return los / np.linalg.norm(los, axis=-1, keepdims=True)
        fixed = azel_to_ecef(site.lat_deg, site.lon_deg, channel.pointing.az_deg, channel.pointing.el_deg)
        return np.broadcast_to(fixed, t.shape + (3,)).copy()

    # ------------------------------------------------------------------ frequency plan

    def channels_watching(self, sat_id: str) -> list[tuple[Site, ReceiveChannel]]:
        """Every channel whose pointing tracks or lists satellite ``sat_id``, in configuration order."""
        self.satellite(sat_id)
        return [
            (site, channel)
            for site in self.sites
            for channel in site.channels
            if sat_id in channel.watched_satellite_ids
        ]

    def transponders_covering(self, f_lo_up: float, f_hi_up: float) -> list[tuple[Satellite, Transponder]]:
        """Every transponder whose uplink passband overlaps the uplink range at all.

        f_lo_up, f_hi_up: the range, Hz (``f_lo_up <= f_hi_up``). A range of non-zero
        width must share more than an edge with the passband; a single frequency
        (``f_lo_up == f_hi_up``) is covered when it lies in the closed passband.
        Returned in configuration order.
        """
        lo, hi = float(f_lo_up), float(f_hi_up)
        if not (math.isfinite(lo) and math.isfinite(hi)):
            raise ValueError("f_lo_up and f_hi_up must be finite")
        if hi < lo:
            raise ValueError(f"f_hi_up ({f_hi_up!r}) must not be below f_lo_up ({f_lo_up!r})")
        found: list[tuple[Satellite, Transponder]] = []
        for satellite in self.satellites:
            for transponder in satellite.transponders:
                if hi == lo:
                    covered = transponder.relays(lo)
                else:
                    covered = min(hi, transponder.uplink_hi_hz) > max(lo, transponder.uplink_lo_hz)
                if covered:
                    found.append((satellite, transponder))
        return found

    def uplink_for(self, sat_id: str, f_down_hz: float) -> tuple[Transponder, float] | None:
        """Map a downlink frequency of a satellite to its transponder and uplink frequency.

        Returns ``(transponder, f_up_hz)`` with ``f_up_hz = f_down_hz - translation_hz``
        for the transponder of ``sat_id`` whose closed downlink passband contains
        ``f_down_hz`` (the one with the nearest downlink centre if passbands overlap,
        the earlier one on a tie), or ``None`` when no passband contains it.
        """
        f_down = float(f_down_hz)
        if not math.isfinite(f_down):
            raise ValueError("f_down_hz must be finite")
        best: Transponder | None = None
        for transponder in self.satellite(sat_id).transponders:
            if transponder.downlink_lo_hz <= f_down <= transponder.downlink_hi_hz:
                if best is None or abs(f_down - transponder.downlink_center_hz) < abs(
                    f_down - best.downlink_center_hz
                ):
                    best = transponder
        if best is None:
            return None
        return best, f_down - best.translation_hz

    def downlink_hz(self, sat_id: str, transponder_id: str, f_up: Any) -> Any:
        """Downlink frequency of an uplink frequency through a transponder:
        ``f_up + translation_hz``, Hz (``f_up`` scalar or array; not limited to the passband)."""
        translation = self.satellite(sat_id).transponder(transponder_id).translation_hz
        if isinstance(f_up, (int, float)):
            return float(f_up) + translation
        return np.asarray(f_up, dtype=np.float64) + translation

    # ------------------------------------------------------------------ uncertainty

    def resolved_uncertainty(self) -> Uncertainty:
        """One-sigma consider uncertainties resolved per satellite and per site.

        Satellite position and velocity sigmas (RIC; m and m/s) come from
        ``Satellite.ephemeris_sigma`` when set, otherwise from
        ``processing.uncertainty.ephemeris.tle`` for ``tle`` orbits and ``.operator``
        for every other type. Site clocks come from ``Site.clock.sigma_ns``; the
        oscillator fractions and the TDOA bias from ``processing.uncertainty``.
        """
        settings = self.processing.uncertainty
        pos: dict[str, np.ndarray] = {}
        vel: dict[str, np.ndarray] = {}
        for satellite in self.satellites:
            sigma = satellite.ephemeris_sigma
            if sigma is None:
                sigma = settings.ephemeris.tle if satellite.orbit.type == "tle" else settings.ephemeris.operator
            pos[satellite.id] = np.array(sigma.pos_m, dtype=np.float64)
            vel[satellite.id] = np.array(sigma.vel_mps, dtype=np.float64)
        return Uncertainty(
            sat_pos_ric_m=pos,
            sat_vel_ric_mps=vel,
            site_clock_s={site.id: site.clock.sigma_ns * 1e-9 for site in self.sites},
            site_osc_frac={site.id: settings.site_osc_frac for site in self.sites},
            sat_osc_frac={satellite.id: settings.sat_osc_frac for satellite in self.satellites},
            tdoa_bias_s=settings.tdoa_bias_ns * 1e-9,
        )

    # ------------------------------------------------------------------ export and advice

    def export_dict(self) -> dict[str, Any]:
        """The configuration as plain JSON-compatible data that loads back to an equal model.

        Unset optional fields are left out, and so are monitors that were derived (they
        are derived again on load, following the transponders).
        """
        data = self.model_dump(mode="json", exclude_none=True)
        for site_data, site in zip(data["sites"], self.sites):
            for channel_data, channel in zip(site_data["channels"], site.channels):
                if channel._monitors_derived:
                    channel_data["monitors"] = []
        return data

    def warnings(self, t_s: float | None = None) -> list[dict[str, str]]:
        """Things that are valid but deserve the operator's attention: ``[{"loc", "msg"}]``
        (the shape of ``field_errors``).

        - a parabolic antenna under 3 wavelengths at a downlink it watches (it behaves
          as a reflector with a broad beam, not as a focusing dish);
        - with ``t_s`` (Unix seconds) given: a watched satellite that is not above the
          site's horizon at that time (its downlink never appears in the channel);
        - a carrier whose allocation reaches beyond the uplink passband of its
          transponder (it is relayed, its centre being inside, but not all of it);
        - a channel with configured (not derived) monitors none of which overlaps the
          downlink passband of a transponder of a satellite it watches: nothing on that
          transponder can be detected through the channel. Configured monitors do not
          follow the frequency plan, so this is what a transponder moved after the
          monitors were written down looks like.
        """
        notes: list[dict[str, str]] = []
        sats = {satellite.id: satellite for satellite in self.satellites}
        orbits = self.orbits() if t_s is not None else {}
        for k, carrier in enumerate(self.carriers):
            transponder = sats[carrier.satellite_id].transponder(carrier.transponder_id)
            if (
                carrier.uplink_lo_hz < transponder.uplink_lo_hz - 1e-6
                or carrier.uplink_hi_hz > transponder.uplink_hi_hz + 1e-6
            ):
                notes.append(
                    {
                        "loc": f"carriers.{k}.bandwidth_hz",
                        "msg": f"carrier {carrier.id!r} ({carrier.uplink_lo_hz:.0f} to {carrier.uplink_hi_hz:.0f} Hz) "
                        f"extends beyond the uplink passband of transponder {transponder.id!r} "
                        f"({transponder.uplink_lo_hz:.0f} to {transponder.uplink_hi_hz:.0f} Hz)",
                    }
                )
        for s, site in enumerate(self.sites):
            for c, channel in enumerate(site.channels):
                base = f"sites.{s}.channels.{c}"
                if not channel.monitors_derived:
                    windows = [
                        (monitor.center_hz - 0.5 * monitor.span_hz, monitor.center_hz + 0.5 * monitor.span_hz)
                        for monitor in channel.monitors
                    ]
                    for sat_id in channel.watched_satellite_ids:
                        for transponder in sats[sat_id].transponders:
                            lo, hi = transponder.downlink_lo_hz, transponder.downlink_hi_hz
                            if not any(w_lo < hi and w_hi > lo for w_lo, w_hi in windows):
                                notes.append(
                                    {
                                        "loc": f"{base}.monitors",
                                        "msg": f"channel {channel.id!r}: no monitor window overlaps the downlink of "
                                        f"transponder {transponder.id!r} of satellite {sat_id!r} "
                                        f"({lo:.0f} to {hi:.0f} Hz); nothing on it can be detected through this channel",
                                    }
                                )
                for sat_id in channel.watched_satellite_ids:
                    downlinks = [tp.downlink_center_hz for tp in sats[sat_id].transponders]
                    if downlinks and channel.antenna.type == "parabolic":
                        lowest = min(downlinks)
                        if electrically_small(channel.antenna, lowest):
                            notes.append(
                                {
                                    "loc": f"{base}.antenna",
                                    "msg": f"channel {channel.id!r}: a {channel.antenna.diameter_m:g} m dish is "
                                    f"under 3 wavelengths at {lowest / 1e6:.3f} MHz ({sat_id}); it behaves as a "
                                    "reflector with a broad beam, not as a focusing dish",
                                }
                            )
                    if t_s is not None:
                        elevation = float(look_angles(site.xyz(), orbit_position(orbits[sat_id], float(t_s)))[1])
                        if elevation <= 0.0:
                            notes.append(
                                {
                                    "loc": f"{base}.pointing",
                                    "msg": f"channel {channel.id!r}: satellite {sat_id!r} is {-elevation:.1f} "
                                    f"degrees below the horizon of site {site.id!r}",
                                }
                            )
        return notes


def _dotted(loc: Sequence[Union[str, int]]) -> str:
    return ".".join(str(part) for part in loc)


_UNION_TAGS: dict[str, frozenset[str]] = {
    "orbit": frozenset({"tle", "geo", "kepler", "table"}),
    "antenna": frozenset({"isotropic", "parabolic", "beam", "table"}),
    "pointing": frozenset({"track", "fixed", "omni", "azel"}),
}


def field_errors(exc: ValidationError) -> list[dict[str, str]]:
    """The problems of a failed validation as ``[{"loc": "sites.0.lat_deg", "msg": "..."}]``.

    ``loc`` is the dotted path of the field in the configuration document (list
    positions as numbers; ``"(root)"`` for the document itself). Pydantic inserts the
    name of the chosen union member into its own locations (``orbit.geo.lon_deg``,
    ``pointing.track.satellite_id``); those are removed here so that every path can be
    followed in the document as written.
    """
    problems: list[dict[str, str]] = []
    for error in exc.errors(include_url=False, include_context=False, include_input=False):
        parts: list[str] = []
        previous: Any = None
        for part in error["loc"]:
            if isinstance(part, str) and isinstance(previous, str) and part in _UNION_TAGS.get(previous, ()):
                previous = None
                continue
            parts.append(str(part))
            previous = part
        problems.append({"loc": ".".join(parts) or "(root)", "msg": str(error["msg"])})
    return problems
