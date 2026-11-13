"""Simulation scenario models (ARCHITECTURE.md section 4).

A scenario says what is radiating and how the simulated world differs from what the
system believes (truth errors). It is separate from ``SystemConfig`` on purpose: the
configuration is what an operator knows, the scenario is what is true.

The field names and JSON shape here are a contract shared with the operator console
(``server/api/schemas.py``) and the simulation backplane (PROTOCOL.md section A).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from petal_locate.config.loader import YamlLimitError, safe_yaml_load, unicode_problem
from petal_locate.config.models import (
    Antenna,
    EmitterPointing,
    Ident,
    IsoTime,
    LatDeg,
    LonDeg,
    NonNegativeFloat,
    PositiveFloat,
    Vector3,
)
from petal_locate.core.timebase import ns_from_iso

__all__ = [
    "EmitterSpec",
    "EphemerisError",
    "IonoError",
    "TropoError",
    "Scenario",
    "ScenarioError",
    "TruthErrors",
    "WaveformSpec",
    "dump_scenario",
    "load_scenario",
    "loads_scenario",
]


class ScenarioError(ValueError):
    """A scenario that cannot be loaded or does not fit the configuration."""


class _Model(BaseModel):
    """Unknown fields and non-finite numbers are errors."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class WaveformSpec(_Model):
    """What an emitter transmits (MATH.md section 4, deterministic waveforms).

    type: ``cw`` (carrier), ``noise`` (band-limited Gaussian noise), ``chirp``
    (linear sweep across ``bandwidth_hz`` every ``sweep_period_s``) or ``psk``
    (root-raised-cosine PSK of ``order`` at ``symbol_rate_hz`` with ``rolloff``).
    bandwidth_hz: occupied bandwidth, Hz (noise and chirp; ignored for cw; for psk it
    is derived as ``symbol_rate_hz * (1 + rolloff)`` when left at 0).
    duty, period_s: on/off gating; ``duty = 1`` or ``period_s = 0`` is continuous.
    seed: distinguishes otherwise identical emitters.
    """

    type: Literal["cw", "noise", "chirp", "psk"] = "noise"
    bandwidth_hz: NonNegativeFloat = 0.0
    sweep_period_s: PositiveFloat = 0.01
    symbol_rate_hz: NonNegativeFloat = 0.0
    order: Literal[2, 4, 8] = 4
    rolloff: float = Field(default=0.35, ge=0.0, le=1.0)
    duty: float = Field(default=1.0, gt=0.0, le=1.0)
    period_s: NonNegativeFloat = 0.0
    seed: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _check(self) -> WaveformSpec:
        if self.type in ("noise", "chirp") and self.bandwidth_hz <= 0.0:
            raise ValueError(f"bandwidth_hz: must be positive for a {self.type!r} waveform")
        if self.type == "psk" and self.symbol_rate_hz <= 0.0 and self.bandwidth_hz <= 0.0:
            raise ValueError("symbol_rate_hz: a psk waveform needs symbol_rate_hz or bandwidth_hz")
        if self.duty < 1.0 and self.period_s <= 0.0:
            raise ValueError("period_s: must be positive when duty < 1")
        return self


class EmitterSpec(_Model):
    """One radiating source in the simulated world.

    kind: ``jammer`` (interference reaching satellites), ``carrier`` (an authorised
    carrier of the configuration; set ``carrier_id``) or ``local`` (a terrestrial
    interferer heard directly by nearby sites).
    carrier_id: the configuration ``Carrier`` this emitter realises. When set, every
    field left as ``None`` takes the carrier's value (location, ``freq_hz`` =
    ``uplink_center_hz``, EIRP, antenna, pointing, waveform from its ``EmitterModel``).
    lat_deg, lon_deg, alt_m, antenna_height_m: position; the phase centre is at
    ``alt_m + antenna_height_m`` above the ellipsoid.
    freq_hz: RF centre frequency actually radiated, Hz.
    eirp_dbw: EIRP on boresight, dBW.
    t_start, t_stop: optional on/off times (UTC); absent means always.
    """

    id: Ident
    name: str = ""
    kind: Literal["jammer", "carrier", "local"] = "jammer"
    carrier_id: Ident | None = None
    lat_deg: LatDeg | None = None
    lon_deg: LonDeg | None = None
    alt_m: float | None = None
    antenna_height_m: NonNegativeFloat | None = None
    freq_hz: PositiveFloat | None = None
    eirp_dbw: float | None = None
    antenna: Antenna | None = None
    pointing: EmitterPointing | None = None
    waveform: WaveformSpec | None = None
    enabled: bool = True
    t_start: IsoTime | None = None
    t_stop: IsoTime | None = None

    @model_validator(mode="after")
    def _check(self) -> EmitterSpec:
        if self.kind == "carrier" and self.carrier_id is None:
            raise ValueError("carrier_id: required when kind is 'carrier'")
        if self.t_start is not None and self.t_stop is not None:
            if ns_from_iso(self.t_stop) <= ns_from_iso(self.t_start):
                raise ValueError("t_stop: must be later than t_start")
        if self.carrier_id is None:
            missing = [
                name
                for name in ("lat_deg", "lon_deg", "freq_hz", "eirp_dbw", "waveform")
                if getattr(self, name) is None
            ]
            if missing:
                raise ValueError(f"{missing[0]}: required for an emitter without carrier_id")
        return self


class EphemerisError(_Model):
    """True orbit minus believed orbit for one satellite (``core.orbits.PerturbedOrbit``):
    ``ric_pos_m`` (m) in the believed orbit's radial / in-track / cross-track frame,
    growing at ``ric_vel_mps`` (m/s) from ``t_ref``. ``t_ref`` matters only when the
    velocity offset is non-zero; ``SimWorld`` then requires it (or an ``epoch_ns`` of
    its own), since an offset growing from an arbitrary epoch would be meaningless."""

    ric_pos_m: Vector3 = Field(default_factory=lambda: [0.0, 0.0, 0.0])
    ric_vel_mps: Vector3 = Field(default_factory=lambda: [0.0, 0.0, 0.0])
    t_ref: IsoTime | None = None


class IonoError(_Model):
    """Ionospheric group delay applied to every path, one VTEC (TECU) at every ground end
    (the solver carries it as a consider parameter, ``processing.uncertainty.iono_vtec_tecu``)."""

    enabled: bool = False
    vtec_tecu: NonNegativeFloat = 30.0


class TropoError(_Model):
    """Tropospheric delay applied to every leg of every path (envelope and carrier phase
    alike), ``zenith_m * m(el) / C`` with the mapping of ``core.media.tropo_mapping``, one
    zenith delay (m) at every ground end (the solver carries it as a consider parameter,
    ``processing.uncertainty.tropo_zenith_m``). The direct terrestrial path gets none."""

    enabled: bool = False
    zenith_m: NonNegativeFloat = 2.3


class TruthErrors(_Model):
    """How the simulated world differs from what the system believes.

    ephemeris: per satellite id. sat_osc_frac: fractional error of each satellite's
    translation oscillator. site_clock_offset_ns: ``delta_g`` per site, true time
    minus labelled time, ns. site_osc_frac: fractional error of each site's reference
    oscillator (``eps_g``). iono: ionospheric delay. tropo: tropospheric delay.
    """

    ephemeris: dict[str, EphemerisError] = Field(default_factory=dict)
    sat_osc_frac: dict[str, float] = Field(default_factory=dict)
    site_clock_offset_ns: dict[str, float] = Field(default_factory=dict)
    site_osc_frac: dict[str, float] = Field(default_factory=dict)
    iono: IonoError = Field(default_factory=IonoError)
    tropo: TropoError = Field(default_factory=TropoError)


class Scenario(_Model):
    """A simulated world: emitters plus truth errors."""

    name: str
    seed: int = Field(default=1, ge=0)
    description: str = ""
    emitters: list[EmitterSpec] = Field(default_factory=list)
    errors: TruthErrors = Field(default_factory=TruthErrors)

    @model_validator(mode="before")
    @classmethod
    def _text_is_unicode(cls, data: Any) -> Any:
        """Refuse text no reply could write out again (an unpaired surrogate)."""
        if isinstance(data, dict):
            problem = unicode_problem(data)
            if problem is not None:
                raise ValueError(f"{problem['loc']}: {problem['msg']}")
        return data

    @model_validator(mode="after")
    def _check(self) -> Scenario:
        seen: set[str] = set()
        for i, emitter in enumerate(self.emitters):
            if emitter.id in seen:
                raise ValueError(f"emitters[{i}].id: duplicate emitter id {emitter.id!r}")
            seen.add(emitter.id)
        return self

    def emitter(self, emitter_id: str) -> EmitterSpec:
        """The scenario emitter with this id; ``ScenarioError`` naming it when there is none."""
        for emitter in self.emitters:
            if emitter.id == emitter_id:
                return emitter
        raise ScenarioError(f"emitter_id {emitter_id!r} is not an emitter of scenario {self.name!r}")

    def validate_against(self, config: Any) -> None:
        """Raise ``ScenarioError`` naming the field when the scenario refers to a
        satellite, site or carrier the configuration does not have, when an emitter
        that realises a carrier cannot take its defaults from it, when an emitter id
        collides with the emitter the simulator creates for a configuration carrier
        (one with ``location`` and ``emitter`` that no scenario emitter names; its id is
        the carrier id), or when a waveform asks for what the synthesiser cannot make
        (8-PSK: ``core.signals`` makes BPSK and QPSK)."""
        sat_ids = {s.id for s in config.satellites}
        site_ids = {s.id for s in config.sites}
        carriers = {c.id: c for c in config.carriers}
        named = {e.carrier_id for e in self.emitters if e.carrier_id is not None}
        auto_ids = {
            c.id for c in config.carriers if c.id not in named and c.location is not None and c.emitter is not None
        }
        for i, emitter in enumerate(self.emitters):
            where = f"emitters[{i}] ({emitter.id})"
            if emitter.id in auto_ids:
                raise ScenarioError(
                    f"{where}.id: {emitter.id!r} is the id of the emitter created for configuration carrier "
                    f"{emitter.id!r}; rename it or set carrier_id: {emitter.id} to realise that carrier"
                )
            if emitter.waveform is not None and emitter.waveform.type == "psk" and emitter.waveform.order not in (2, 4):
                raise ScenarioError(
                    f"{where}.waveform.order: {emitter.waveform.order}-PSK cannot be synthesised (2 or 4 only)"
                )
            if emitter.carrier_id is not None:
                carrier = carriers.get(emitter.carrier_id)
                if carrier is None:
                    raise ScenarioError(f"{where}.carrier_id: unknown carrier {emitter.carrier_id!r}")
                if carrier.location is None and (emitter.lat_deg is None or emitter.lon_deg is None):
                    raise ScenarioError(
                        f"{where}: carrier {carrier.id!r} has no location, so lat_deg and lon_deg are required"
                    )
                if carrier.emitter is None and (emitter.eirp_dbw is None or emitter.waveform is None):
                    raise ScenarioError(
                        f"{where}: carrier {carrier.id!r} has no emitter model, so eirp_dbw and waveform are required"
                    )
            pointing = emitter.pointing
            target = getattr(pointing, "satellite_id", None)
            if target is not None and target not in sat_ids:
                raise ScenarioError(f"{where}.pointing.satellite_id: unknown satellite {target!r}")
        for field_name, known, what in (
            ("ephemeris", sat_ids, "satellite"),
            ("sat_osc_frac", sat_ids, "satellite"),
            ("site_clock_offset_ns", site_ids, "site"),
            ("site_osc_frac", site_ids, "site"),
        ):
            for key in getattr(self.errors, field_name):
                if key not in known:
                    raise ScenarioError(f"errors.{field_name}: unknown {what} {key!r}")


def loads_scenario(text: str) -> Scenario:
    """Parse a scenario from YAML or JSON text. Raises ``ScenarioError`` (also for a
    document nested too deeply, one whose aliases expand beyond
    ``config.loader.MAX_YAML_NODES``, and text that is not valid Unicode)."""
    try:
        data = safe_yaml_load(text)
    except YamlLimitError as exc:
        raise ScenarioError(f"(root): {exc}") from None
    except (yaml.YAMLError, ValueError) as exc:  # ValueError: e.g. an impossible unquoted date
        raise ScenarioError(f"not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ScenarioError("a scenario must be a mapping")
    problem = unicode_problem(data)
    if problem is not None:
        raise ScenarioError(f"{problem['loc']}: {problem['msg']}")
    try:
        return Scenario.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first["loc"])
        raise ScenarioError(f"{loc}: {first['msg']}") from exc


def load_scenario(path: str | Path) -> Scenario:
    """Load a scenario file (YAML or JSON). Raises ``ScenarioError`` naming the file."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ScenarioError(f"{p}: {exc.strerror or exc}") from exc
    try:
        return loads_scenario(text)
    except ScenarioError as exc:
        raise ScenarioError(f"{p}: {exc}") from exc


def dump_scenario(scenario: Scenario) -> str:
    """YAML text that ``loads_scenario`` reads back to an equal scenario."""
    return yaml.safe_dump(scenario.model_dump(mode="json", exclude_none=True), sort_keys=False)
