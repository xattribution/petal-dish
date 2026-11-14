"""Satellite orbits (MATH.md section 2).

Every orbit exposes ``state_ecef(t_s) -> (r, v)`` with ``t_s`` in float Unix seconds
(UTC), scalar or an array of any shape ``S``; ``r`` and ``v`` are ECEF (WGS84) metres
and metres per second with shape ``S + (3,)``.

The orbits defined here also expose ``position_ecef`` (the same position without the
velocity work), and both of their methods take a second argument ``dt_s``: the state is
evaluated at ``t_s + dt_s`` *without forming that sum in float64*. A float64 near
1.8e9 s resolves 2.4e-7 s, during which a satellite in low orbit moves 2 mm; the
light-time code needs the position at ``t_rx - tau`` exactly, so it passes the large
time and the small offset separately (``orbit_position(orbit, t_s, dt_s)``). Orbits
that only implement the protocol are evaluated at the rounded sum.

Times are Unix *seconds*. A time whose magnitude exceeds ``timebase.MAX_UNIX_S`` (1e11 s,
year 5138) is rejected with ValueError: it is an integer ``t_ns`` (or milliseconds) that
missed ``timebase.sec``, and would otherwise produce a plausible-looking state.

``v`` is the time derivative of the ECEF position for every type except ``tle``, whose
SGP4 velocity differs from ``dr/dt`` by up to 0.06 m/s. Delay rates never use ``v``
(MATH.md section 3); it builds RIC frames and is shown to operators.
"""
from __future__ import annotations

import datetime as _dt
from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

import numpy as np

from .constants import GM, J2, JD_UNIX_EPOCH, OMEGA_E, OMEGA_GMST, WGS84_A
from .geodesy import ecef_to_lla
from .timebase import MAX_UNIX_S, gmst_rad, ns_from_datetime, ns_from_iso, sec, wrapped_phase

__all__ = [
    "Orbit",
    "TleOrbit",
    "GeoOrbit",
    "KeplerOrbit",
    "TableOrbit",
    "PerturbedOrbit",
    "orbit_from_config",
    "orbit_position",
    "orbit_state",
    "subsatellite_point",
    "tle_checksum",
]

_TWO_PI = 2.0 * np.pi
_SECONDS_PER_DAY = 86400.0
_DEFAULT_GEO_EPOCH = "2026-01-01T00:00:00Z"

# PerturbedOrbit velocity: 5-point central difference of the perturbed position. The
# step is a power of two so t +/- h is exact in float64 at Unix-epoch magnitudes.
_VEL_STEP_S = 0.125
_VEL_OFFSETS = np.array([-2.0, -1.0, 1.0, 2.0]) * _VEL_STEP_S
_VEL_WEIGHTS = np.array([1.0, -8.0, 8.0, -1.0]) / (12.0 * _VEL_STEP_S)


@runtime_checkable
class Orbit(Protocol):
    """Anything that can report an ECEF state."""

    def state_ecef(self, t_s: Any) -> tuple[np.ndarray, np.ndarray]:
        """ECEF position (m) and velocity (m/s) at Unix seconds ``t_s``; shapes ``S + (3,)``."""
        ...


def orbit_position(orbit: Orbit, t_s: Any, dt_s: Any = 0.0) -> np.ndarray:
    """ECEF position (m) of ``orbit`` at Unix seconds ``t_s + dt_s``.

    t_s and dt_s broadcast to a shape ``S``; the result is ``S + (3,)``. Orbits that
    declare ``supports_split_time`` receive the two terms separately and lose nothing
    to the rounding of the sum; any other object satisfying the ``Orbit`` protocol is
    evaluated at ``float64(t_s + dt_s)`` through ``position_ecef`` if it has one and
    ``state_ecef`` otherwise.
    """
    if getattr(orbit, "supports_split_time", False):
        return orbit.position_ecef(t_s, dt_s)  # type: ignore[attr-defined]
    t = np.asarray(t_s, dtype=np.float64) + np.asarray(dt_s, dtype=np.float64)
    fast = getattr(orbit, "position_ecef", None)
    if fast is not None:
        return np.asarray(fast(t), dtype=np.float64)
    return np.asarray(orbit.state_ecef(t)[0], dtype=np.float64)


def orbit_state(orbit: Orbit, t_s: Any, dt_s: Any = 0.0) -> tuple[np.ndarray, np.ndarray]:
    """ECEF position (m) and velocity (m/s) of ``orbit`` at ``t_s + dt_s`` (see ``orbit_position``)."""
    if getattr(orbit, "supports_split_time", False):
        return orbit.state_ecef(t_s, dt_s)  # type: ignore[call-arg]
    t = np.asarray(t_s, dtype=np.float64) + np.asarray(dt_s, dtype=np.float64)
    r, v = orbit.state_ecef(t)
    return np.asarray(r, dtype=np.float64), np.asarray(v, dtype=np.float64)


def subsatellite_point(orbit: Orbit, t_s: Any) -> tuple[Any, Any, Any]:
    """Geodetic sub-satellite point: ``(lat_deg, lon_deg, alt_m)`` at Unix seconds ``t_s``.

    Longitude in ``[-180, 180)``; ``alt_m`` is the satellite's height above the WGS84
    ellipsoid. Shapes follow ``t_s``.
    """
    return ecef_to_lla(orbit_position(orbit, t_s))


# --------------------------------------------------------------------------- TLE / SGP4


def tle_checksum(line: str) -> int:
    """Modulo-10 checksum of the first 68 characters of a TLE line (digits, '-' counts 1)."""
    total = 0
    for ch in line[:68]:
        if ch.isdigit():
            total += int(ch)
        elif ch == "-":
            total += 1
    return total % 10


class TleOrbit:
    """SGP4 propagation of a two-line element set, rotated from TEME to ECEF.

    line1, line2: the element set (69 characters each, checksums verified).
    dut1_s: UT1 - UTC in seconds for the Earth rotation angle.

    SGP4 is called with split time (``jd = 2440587.5 + floor(t_s / 86400)``,
    ``fr = (t_s mod 86400) / 86400``). ``r_ecef = Rz(-theta) r_teme`` and
    ``v_ecef = Rz(-theta) v_teme - [0, 0, OMEGA_GMST] x r_ecef`` with
    ``theta = gmst_rad(t_s, dut1_s)``; polar motion is ignored. A small offset ``dt_s``
    is added to ``fr`` and to the rotation angle, not to ``t_s``.
    """

    supports_split_time = True

    def __init__(self, line1: str, line2: str, dut1_s: float = 0.0) -> None:
        from sgp4.api import Satrec

        l1 = _check_tle_line(line1, "line1", "1")
        l2 = _check_tle_line(line2, "line2", "2")
        if l1[2:7] != l2[2:7]:
            raise ValueError("line2 carries a different catalog number from line1")
        try:
            self._sat = Satrec.twoline2rv(l1, l2)
        except Exception as exc:  # sgp4 raises ValueError on malformed fields
            raise ValueError(f"line1/line2 could not be parsed as a TLE: {exc}") from None
        self.line1 = l1
        self.line2 = l2
        self.dut1_s = float(dut1_s)

    @property
    def epoch_s(self) -> float:
        """Element-set epoch, Unix seconds (UTC)."""
        whole = self._sat.jdsatepoch - JD_UNIX_EPOCH
        return float(whole * _SECONDS_PER_DAY + self._sat.jdsatepochF * _SECONDS_PER_DAY)

    def _teme(
        self, t_s: Any, dt_s: Any
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, tuple[int, ...]]:
        t, extra = _times(t_s, dt_s)
        flat = np.ascontiguousarray(t.reshape(-1))
        extra = extra.reshape(-1)
        whole_days = np.floor(flat / _SECONDS_PER_DAY)
        jd = JD_UNIX_EPOCH + whole_days
        fr = ((flat - whole_days * _SECONDS_PER_DAY) + extra) / _SECONDS_PER_DAY
        err, r_km, v_kms = self._sat.sgp4_array(jd, np.ascontiguousarray(fr))
        if np.any(err != 0):
            k = int(np.flatnonzero(err)[0])
            raise ValueError(
                f"t_s: SGP4 cannot propagate this TLE to t_s={float(flat[k])!r} "
                f"(sgp4 error code {int(err[k])})"
            )
        theta = gmst_rad(flat, self.dut1_s + extra)
        return theta, np.asarray(r_km) * 1e3, np.asarray(v_kms) * 1e3, t.shape

    def position_ecef(self, t_s: Any, dt_s: Any = 0.0) -> np.ndarray:
        """ECEF position at ``t_s + dt_s``, m, shape ``S + (3,)``."""
        theta, r_teme, _, shape = self._teme(t_s, dt_s)
        return _rot_z(r_teme, -theta).reshape(shape + (3,))

    def state_ecef(self, t_s: Any, dt_s: Any = 0.0) -> tuple[np.ndarray, np.ndarray]:
        """ECEF position (m) and velocity (m/s) at ``t_s + dt_s``, shapes ``S + (3,)``."""
        theta, r_teme, v_teme, shape = self._teme(t_s, dt_s)
        r = _rot_z(r_teme, -theta)
        v = _rot_z(v_teme, -theta) - _omega_cross(OMEGA_GMST, r)
        return r.reshape(shape + (3,)), v.reshape(shape + (3,))


def _check_tle_line(line: Any, name: str, number: str) -> str:
    if not isinstance(line, str):
        raise ValueError(f"{name} must be a string")
    text = line.rstrip("\r\n")
    if len(text) > 69:
        text = text.rstrip()  # tolerate trailing blanks only
    if len(text) != 69:
        raise ValueError(f"{name} must be 69 characters long, got {len(text)}")
    if text[0] != number:
        raise ValueError(f"{name} must start with {number!r}")
    if not text[68].isdigit() or int(text[68]) != tle_checksum(text):
        raise ValueError(f"{name} checksum mismatch: expected {tle_checksum(text)}, found {text[68]!r}")
    return text


# --------------------------------------------------------------------------- analytic GEO


class GeoOrbit:
    """Analytic circular geosynchronous orbit for slot-style definitions.

    lon_deg: longitude the ground track is centred on, degrees east.
    inclination_deg: orbit inclination, degrees (0 = geostationary).
    epoch: reference time, ISO-8601 string, ``datetime`` or float Unix seconds.
    phase_deg: argument of latitude ``u`` at the epoch, degrees.
    drift_deg_per_day: eastward drift of the mean longitude, degrees per 86400 s.

    ``n = OMEGA_E + drift``, ``a = (GM / n^2)^(1/3)``, ``u(t) = phase + n (t - epoch)``,
    ``r_ecef = Rz(-OMEGA_E (t - epoch)) Rz(lon - phase) Rx(inc) [a cos u, a sin u, 0]``.
    Evaluated in the algebraically identical form

        L = lon + drift (t - epoch),  w = lon - phase - OMEGA_E (t - epoch),  q = 1 - cos(inc)
        r = a [cos L + q sin w sin u,  sin L - q cos w sin u,  sin(inc) sin u]

    which is exactly constant (and has exactly zero velocity) for zero inclination and
    drift. The velocity is the analytic derivative of this position.
    """

    supports_split_time = True

    def __init__(
        self,
        lon_deg: float,
        inclination_deg: float = 0.0,
        epoch: Any = _DEFAULT_GEO_EPOCH,
        phase_deg: float = 0.0,
        drift_deg_per_day: float = 0.0,
    ) -> None:
        self.lon_deg = _finite(lon_deg, "lon_deg")
        self.inclination_deg = _finite(inclination_deg, "inclination_deg")
        if not 0.0 <= self.inclination_deg <= 180.0:
            raise ValueError("inclination_deg must be within [0, 180]")
        self.phase_deg = _finite(phase_deg, "phase_deg")
        self.drift_deg_per_day = _finite(drift_deg_per_day, "drift_deg_per_day")
        self.epoch_s = _epoch_seconds(epoch, "epoch")
        self._drift = np.radians(self.drift_deg_per_day) / _SECONDS_PER_DAY
        self.mean_motion_rad_s = OMEGA_E + self._drift
        if self.mean_motion_rad_s <= 0.0:
            raise ValueError("drift_deg_per_day makes the mean motion non-positive")
        self.a_m = float((GM / self.mean_motion_rad_s**2) ** (1.0 / 3.0))
        inc = np.radians(self.inclination_deg)
        self._sin_i = float(np.sin(inc))
        self._q = float(2.0 * np.sin(0.5 * inc) ** 2)
        self._lon = np.radians(self.lon_deg)
        self._phase = np.radians(self.phase_deg)

    def _angles(self, t_s: Any, dt_s: Any) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        t, extra = _times(t_s, dt_s)
        dt = t - self.epoch_s  # exact for any time within a factor of two of the epoch
        rot = wrapped_phase(OMEGA_E, dt) + OMEGA_E * extra
        if self._drift == 0.0:
            big_l = np.full(dt.shape, self._lon)
            u = self._phase + rot
        else:
            big_l = self._lon + self._drift * (dt + extra)
            u = self._phase + (wrapped_phase(self.mean_motion_rad_s, dt) + self.mean_motion_rad_s * extra)
        w = (self._lon - self._phase) - rot
        return big_l, w, u

    def position_ecef(self, t_s: Any, dt_s: Any = 0.0) -> np.ndarray:
        """ECEF position at ``t_s + dt_s``, m, shape ``S + (3,)``."""
        big_l, w, u = self._angles(t_s, dt_s)
        su = np.sin(u)
        a, q = self.a_m, self._q
        return np.stack(
            [
                a * (np.cos(big_l) + q * np.sin(w) * su),
                a * (np.sin(big_l) - q * np.cos(w) * su),
                a * self._sin_i * su,
            ],
            axis=-1,
        )

    def state_ecef(self, t_s: Any, dt_s: Any = 0.0) -> tuple[np.ndarray, np.ndarray]:
        """ECEF position (m) and its exact time derivative (m/s) at ``t_s + dt_s``, shapes ``S + (3,)``."""
        big_l, w, u = self._angles(t_s, dt_s)
        su, cu = np.sin(u), np.cos(u)
        sw, cw = np.sin(w), np.cos(w)
        sl, cl = np.sin(big_l), np.cos(big_l)
        a, q, n, nd = self.a_m, self._q, self.mean_motion_rad_s, self._drift
        r = np.stack(
            [a * (cl + q * sw * su), a * (sl - q * cw * su), a * self._sin_i * su], axis=-1
        )
        v = np.stack(
            [
                a * (-nd * sl + q * (n * sw * cu - OMEGA_E * cw * su)),
                a * (nd * cl - q * (n * cw * cu + OMEGA_E * sw * su)),
                a * n * self._sin_i * cu,
            ],
            axis=-1,
        )
        return r, v


# --------------------------------------------------------------------------- Kepler + J2


class KeplerOrbit:
    """Classical elements at an epoch: two-body motion with secular J2.

    a_m: semi-major axis, m. e: eccentricity in [0, 1). inc_deg, raan_deg, argp_deg,
    mean_anomaly_deg: inclination, right ascension of the ascending node, argument of
    perigee and mean anomaly at the epoch, degrees, in the inertial (true-equator)
    frame that ``gmst_rad`` rotates to ECEF. epoch: ISO-8601 string, ``datetime`` or
    float Unix seconds. j2: apply the first-order secular rates. dut1_s: UT1 - UTC, s.

    With ``p = a (1 - e^2)``, ``n0 = sqrt(GM / a^3)`` and ``k = 1.5 J2 (WGS84_A / p)^2``:

        n      = n0 (1 + k sqrt(1 - e^2) (1 - 1.5 sin^2 i))       mean-anomaly rate
        raan'  = -k n cos i
        argp'  =  k n (2 - 2.5 sin^2 i)

    (all zero corrections when ``j2`` is false). The velocity is the exact time
    derivative of the returned ECEF position, including the node, perigee and Earth
    rotation rates.
    """

    supports_split_time = True

    def __init__(
        self,
        a_m: float,
        e: float,
        inc_deg: float,
        raan_deg: float,
        argp_deg: float,
        mean_anomaly_deg: float,
        epoch: Any,
        j2: bool = True,
        dut1_s: float = 0.0,
    ) -> None:
        self.a_m = _finite(a_m, "a_m")
        if self.a_m <= 0.0:
            raise ValueError("a_m must be positive")
        self.e = _finite(e, "e")
        if not 0.0 <= self.e < 1.0:
            raise ValueError("e must be within [0, 1)")
        self.inc_deg = _finite(inc_deg, "inc_deg")
        if not 0.0 <= self.inc_deg <= 180.0:
            raise ValueError("inc_deg must be within [0, 180]")
        self.raan_deg = _finite(raan_deg, "raan_deg")
        self.argp_deg = _finite(argp_deg, "argp_deg")
        self.mean_anomaly_deg = _finite(mean_anomaly_deg, "mean_anomaly_deg")
        self.epoch_s = _epoch_seconds(epoch, "epoch")
        self.j2 = bool(j2)
        self.dut1_s = float(dut1_s)

        inc = np.radians(self.inc_deg)
        self._sin_i, self._cos_i = float(np.sin(inc)), float(np.cos(inc))
        self._beta = float(np.sqrt(1.0 - self.e * self.e))
        n0 = float(np.sqrt(GM / self.a_m**3))
        if self.j2:
            p = self.a_m * (1.0 - self.e * self.e)
            k = 1.5 * J2 * (WGS84_A / p) ** 2
            n = n0 * (1.0 + k * self._beta * (1.0 - 1.5 * self._sin_i**2))
            self.raan_rate_rad_s = float(-k * n * self._cos_i)
            self.argp_rate_rad_s = float(k * n * (2.0 - 2.5 * self._sin_i**2))
        else:
            n = n0
            self.raan_rate_rad_s = 0.0
            self.argp_rate_rad_s = 0.0
        self.mean_motion_rad_s = float(n)
        self._raan0 = np.radians(self.raan_deg)
        self._argp0 = np.radians(self.argp_deg)
        self._m0 = np.radians(self.mean_anomaly_deg)
        # Newton from the convex side converges monotonically. Measured passes to reach a
        # residual of 5e-16 rad: 4 (e < 0.5), 6 (e < 0.9), 9 (e < 0.99), 26 (e = 1 - 1e-9).
        if self.e < 0.5:
            self._kepler_iterations = 6
        elif self.e < 0.9:
            self._kepler_iterations = 8
        elif self.e < 0.99:
            self._kepler_iterations = 12
        else:
            self._kepler_iterations = 40

    def _eccentric_anomaly(self, mean_anomaly: np.ndarray) -> np.ndarray:
        """Solve ``E - e sin E = M`` (radians) by Newton's method, fixed iteration count."""
        e = self.e
        m = np.mod(mean_anomaly + np.pi, _TWO_PI) - np.pi  # [-pi, pi)
        sign = np.where(m < 0.0, -1.0, 1.0)
        m_abs = np.abs(m)
        big_e = np.minimum(m_abs + e, np.pi)  # >= the root, where f is convex and increasing
        for _ in range(self._kepler_iterations):
            big_e = big_e - (big_e - e * np.sin(big_e) - m_abs) / (1.0 - e * np.cos(big_e))
        return sign * big_e

    def _plane(self, t_s: Any, dt_s: Any, want_velocity: bool) -> tuple[np.ndarray, ...]:
        t, extra = _times(t_s, dt_s)
        dt = t - self.epoch_s  # exact for any time within a factor of two of the epoch
        mean_anomaly = self._m0 + (
            wrapped_phase(self.mean_motion_rad_s, dt) + self.mean_motion_rad_s * extra
        )
        argp = self._argp0 + self.argp_rate_rad_s * (dt + extra)
        node = (self._raan0 + self.raan_rate_rad_s * (dt + extra)) - gmst_rad(t, self.dut1_s + extra)
        big_e = self._eccentric_anomaly(mean_anomaly)
        se, ce = np.sin(big_e), np.cos(big_e)
        a, beta = self.a_m, self._beta
        px, py = a * (ce - self.e), a * beta * se
        sw, cw = np.sin(argp), np.cos(argp)
        # in-plane position (ascending node along +x)
        qx, qy = px * cw - py * sw, px * sw + py * cw
        sn, cn = np.sin(node), np.cos(node)
        ci, si = self._cos_i, self._sin_i
        sx, sy, sz = qx, qy * ci, qy * si
        r = np.stack([sx * cn - sy * sn, sx * sn + sy * cn, sz], axis=-1)
        if not want_velocity:
            return (r,)
        e_dot = self.mean_motion_rad_s / (1.0 - self.e * ce)
        pxd, pyd = -a * se * e_dot, a * beta * ce * e_dot
        qxd = pxd * cw - pyd * sw - self.argp_rate_rad_s * qy
        qyd = pxd * sw + pyd * cw + self.argp_rate_rad_s * qx
        sxd, syd, szd = qxd, qyd * ci, qyd * si
        node_rate = self.raan_rate_rad_s - OMEGA_GMST
        v = np.stack(
            [
                sxd * cn - syd * sn - node_rate * r[..., 1],
                sxd * sn + syd * cn + node_rate * r[..., 0],
                szd,
            ],
            axis=-1,
        )
        return r, v

    def position_ecef(self, t_s: Any, dt_s: Any = 0.0) -> np.ndarray:
        """ECEF position at ``t_s + dt_s``, m, shape ``S + (3,)``."""
        return self._plane(t_s, dt_s, want_velocity=False)[0]

    def state_ecef(self, t_s: Any, dt_s: Any = 0.0) -> tuple[np.ndarray, np.ndarray]:
        """ECEF position (m) and its exact time derivative (m/s) at ``t_s + dt_s``, shapes ``S + (3,)``."""
        r, v = self._plane(t_s, dt_s, want_velocity=True)
        return r, v


# --------------------------------------------------------------------------- table


class TableOrbit:
    """Operator ephemeris: ECEF samples interpolated by piecewise cubic Hermite.

    t_s: sample times, float Unix seconds, strictly increasing, ``[N]`` with N >= 2.
    r_m: ECEF positions, m, ``[N, 3]``. v_mps: ECEF velocities (dr/dt), m/s, ``[N, 3]``.
    (``TableOrbit.from_samples`` takes the configuration form, a list of
    ``{t, r_m, v_mps}`` entries with ISO times; so does the constructor when given
    that list alone.)

    The returned velocity is the derivative of the interpolant. Evaluation outside
    ``[t_s[0], t_s[-1]]`` raises ValueError: a table is never extrapolated.
    """

    supports_split_time = True

    def __init__(self, t_s: Any, r_m: Any = None, v_mps: Any = None) -> None:
        if r_m is None and v_mps is None:
            t_s, r_m, v_mps = _unpack_samples(t_s)
        elif r_m is None or v_mps is None:
            raise ValueError("r_m and v_mps must be given together")
        t = np.asarray(t_s, dtype=np.float64)
        r = np.asarray(r_m, dtype=np.float64)
        v = np.asarray(v_mps, dtype=np.float64)
        if t.ndim != 1 or t.size < 2:
            raise ValueError("t_s must be a 1-D array of at least two sample times")
        if r.shape != (t.size, 3):
            raise ValueError(f"r_m must have shape ({t.size}, 3), got {r.shape}")
        if v.shape != (t.size, 3):
            raise ValueError(f"v_mps must have shape ({t.size}, 3), got {v.shape}")
        if not (np.all(np.isfinite(t)) and np.all(np.isfinite(r)) and np.all(np.isfinite(v))):
            raise ValueError("t_s, r_m and v_mps must be finite")
        if float(np.max(np.abs(t))) > MAX_UNIX_S:
            raise ValueError("t_s must be Unix seconds (nanoseconds are not accepted here)")
        if np.any(np.diff(t) <= 0.0):
            raise ValueError("t_s must be strictly increasing")
        self.t_s, self.r_m, self.v_mps = t, r, v

    @classmethod
    def from_samples(cls, samples: Sequence[Any]) -> "TableOrbit":
        """Build from config-style samples with fields ``t`` (ISO or datetime), ``r_m``, ``v_mps``."""
        return cls(*_unpack_samples(samples))

    def _locate(self, t_s: Any, dt_s: Any) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        t, extra = _times(t_s, dt_s)
        when = t + extra
        if when.size and (np.min(when) < self.t_s[0] or np.max(when) > self.t_s[-1]):
            raise ValueError(
                f"t_s outside the ephemeris table span [{float(self.t_s[0])!r}, {float(self.t_s[-1])!r}] "
                f"(asked for {float(np.min(when))!r} .. {float(np.max(when))!r})"
            )
        k = np.clip(np.searchsorted(self.t_s, when, side="right") - 1, 0, self.t_s.size - 2)
        h = self.t_s[k + 1] - self.t_s[k]
        s = ((t - self.t_s[k]) + extra) / h
        return k, h[..., None], s[..., None]

    def position_ecef(self, t_s: Any, dt_s: Any = 0.0) -> np.ndarray:
        """ECEF position at ``t_s + dt_s``, m, shape ``S + (3,)``."""
        k, h, s = self._locate(t_s, dt_s)
        s2 = s * s
        s3 = s2 * s
        return (
            (2.0 * s3 - 3.0 * s2 + 1.0) * self.r_m[k]
            + (s3 - 2.0 * s2 + s) * h * self.v_mps[k]
            + (3.0 * s2 - 2.0 * s3) * self.r_m[k + 1]
            + (s3 - s2) * h * self.v_mps[k + 1]
        )

    def state_ecef(self, t_s: Any, dt_s: Any = 0.0) -> tuple[np.ndarray, np.ndarray]:
        """ECEF position (m) and the derivative of the interpolant (m/s) at ``t_s + dt_s``, shapes ``S + (3,)``."""
        k, h, s = self._locate(t_s, dt_s)
        s2 = s * s
        s3 = s2 * s
        r0, r1, v0, v1 = self.r_m[k], self.r_m[k + 1], self.v_mps[k], self.v_mps[k + 1]
        r = (
            (2.0 * s3 - 3.0 * s2 + 1.0) * r0
            + (s3 - 2.0 * s2 + s) * h * v0
            + (3.0 * s2 - 2.0 * s3) * r1
            + (s3 - s2) * h * v1
        )
        v = (
            (6.0 * s2 - 6.0 * s) * (r0 - r1) / h
            + (3.0 * s2 - 4.0 * s + 1.0) * v0
            + (3.0 * s2 - 2.0 * s) * v1
        )
        return r, v


# --------------------------------------------------------------------------- perturbation


class PerturbedOrbit:
    """A base orbit displaced by a radial / in-track / cross-track (RIC) offset.

    base: the unperturbed orbit. ric_pos_m: offset at ``t_ref_s``, metres ``[3]``
    (radial, in-track, cross-track). ric_vel_mps: rate of change of that offset in the
    rotating RIC frame, m/s ``[3]``. t_ref_s: reference time, float Unix seconds.

        r'(t) = r(t) + Q(t) [ric_pos_m + ric_vel_mps (t - t_ref_s)]

    The columns of ``Q(t)`` are the base orbit's unit vectors at ``t``: radial
    ``r / |r|``, cross-track along ``r x (v_ecef + omega x r)`` and in-track
    ``cross x radial``. The velocity returned is ``d r'/dt``, a fourth-order central
    difference of ``r'`` (step 0.125 s); ``ric_vel_mps`` is never simply added to ``v``.
    ``state_ecef`` therefore needs the base orbit 0.25 s either side of ``t_s``.
    """

    supports_split_time = True

    def __init__(self, base: Orbit, ric_pos_m: Any, ric_vel_mps: Any, t_ref_s: float) -> None:
        if not callable(getattr(base, "state_ecef", None)):
            raise ValueError("base must provide state_ecef(t_s)")
        self.base = base
        self.ric_pos_m = _vector3(ric_pos_m, "ric_pos_m")
        self.ric_vel_mps = _vector3(ric_vel_mps, "ric_vel_mps")
        self.t_ref_s = _finite(t_ref_s, "t_ref_s")
        if abs(self.t_ref_s) > MAX_UNIX_S:
            raise ValueError(
                f"t_ref_s={t_ref_s!r} is not Unix seconds (nanoseconds are not accepted here)"
            )

    def position_ecef(self, t_s: Any, dt_s: Any = 0.0) -> np.ndarray:
        """Perturbed ECEF position at ``t_s + dt_s``, m, shape ``S + (3,)``."""
        t, extra = _times(t_s, dt_s)
        r, v = orbit_state(self.base, t, extra)
        v_inertial = v + _omega_cross(OMEGA_E, r)
        radial = r / np.linalg.norm(r, axis=-1, keepdims=True)
        cross = np.cross(r, v_inertial)
        cross = cross / np.linalg.norm(cross, axis=-1, keepdims=True)
        in_track = np.cross(cross, radial)
        offset = self.ric_pos_m + self.ric_vel_mps * ((t - self.t_ref_s) + extra)[..., None]
        return (
            r
            + offset[..., 0:1] * radial
            + offset[..., 1:2] * in_track
            + offset[..., 2:3] * cross
        )

    def state_ecef(self, t_s: Any, dt_s: Any = 0.0) -> tuple[np.ndarray, np.ndarray]:
        """Perturbed ECEF position (m) and its time derivative (m/s) at ``t_s + dt_s``, shapes ``S + (3,)``."""
        t, extra = _times(t_s, dt_s)
        stencil = self.position_ecef(t[..., None] + _VEL_OFFSETS, extra[..., None])  # S + (4, 3)
        v = np.tensordot(stencil, _VEL_WEIGHTS, axes=([-2], [0]))
        return self.position_ecef(t, extra), v


# --------------------------------------------------------------------------- construction


def orbit_from_config(orbit_model: Any, dut1_s: float = 0.0) -> Orbit:
    """Build an orbit from a configuration object (ARCHITECTURE.md section 3).

    orbit_model: a pydantic model, ``SimpleNamespace``, mapping or any object with the
    fields of the ``Orbit`` config union, selected by ``type``:

        tle    line1, line2
        geo    lon_deg, inclination_deg=0, epoch="2026-01-01T00:00:00Z", phase_deg=0, drift_deg_per_day=0
        kepler a_m, e, inc_deg, raan_deg, argp_deg, mean_anomaly_deg, epoch, j2=True
        table  samples: [{t, r_m[3], v_mps[3]}]   (ECEF)

    Epochs may be ISO-8601 strings or ``datetime`` objects. dut1_s (UT1 - UTC, seconds)
    applies to the inertial types ``tle`` and ``kepler``. An object that already has
    ``state_ecef`` is returned unchanged. Unknown types and missing fields raise
    ValueError naming the field.
    """
    if callable(getattr(orbit_model, "state_ecef", None)):
        return orbit_model
    if not isinstance(orbit_model, Mapping) and not hasattr(orbit_model, "type"):
        orbit_model = getattr(orbit_model, "root", orbit_model)  # a pydantic RootModel around the union
    kind = _field(orbit_model, "type", "orbit.type")
    kind = getattr(kind, "value", kind)  # tolerate enums
    if kind == "tle":
        return TleOrbit(
            _field(orbit_model, "line1", "orbit.line1"),
            _field(orbit_model, "line2", "orbit.line2"),
            dut1_s=dut1_s,
        )
    if kind == "geo":
        return GeoOrbit(
            lon_deg=_field(orbit_model, "lon_deg", "orbit.lon_deg"),
            inclination_deg=_field(orbit_model, "inclination_deg", "orbit.inclination_deg", 0.0),
            epoch=_field(orbit_model, "epoch", "orbit.epoch", _DEFAULT_GEO_EPOCH),
            phase_deg=_field(orbit_model, "phase_deg", "orbit.phase_deg", 0.0),
            drift_deg_per_day=_field(orbit_model, "drift_deg_per_day", "orbit.drift_deg_per_day", 0.0),
        )
    if kind == "kepler":
        return KeplerOrbit(
            a_m=_field(orbit_model, "a_m", "orbit.a_m"),
            e=_field(orbit_model, "e", "orbit.e"),
            inc_deg=_field(orbit_model, "inc_deg", "orbit.inc_deg"),
            raan_deg=_field(orbit_model, "raan_deg", "orbit.raan_deg"),
            argp_deg=_field(orbit_model, "argp_deg", "orbit.argp_deg"),
            mean_anomaly_deg=_field(orbit_model, "mean_anomaly_deg", "orbit.mean_anomaly_deg"),
            epoch=_field(orbit_model, "epoch", "orbit.epoch"),
            j2=_field(orbit_model, "j2", "orbit.j2", True),
            dut1_s=dut1_s,
        )
    if kind == "table":
        return TableOrbit.from_samples(_field(orbit_model, "samples", "orbit.samples"))
    raise ValueError(f"orbit.type must be one of 'tle', 'geo', 'kepler', 'table', got {kind!r}")


# --------------------------------------------------------------------------- helpers

_MISSING: Any = object()


def _unpack_samples(samples: Any) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Config-style table samples ``[{t, r_m, v_mps}, ...]`` -> ``(t_s [N], r_m [N, 3], v_mps [N, 3])``."""
    if isinstance(samples, (str, bytes, Mapping)) or not isinstance(samples, Sequence) or len(samples) < 2:
        raise ValueError("samples must be a list of at least two {t, r_m, v_mps} entries")
    times = [_epoch_seconds(_field(s, "t", "samples[].t"), "samples[].t") for s in samples]
    r = [np.asarray(_field(s, "r_m", "samples[].r_m"), dtype=np.float64) for s in samples]
    v = [np.asarray(_field(s, "v_mps", "samples[].v_mps"), dtype=np.float64) for s in samples]
    if any(x.shape != (3,) for x in r):
        raise ValueError("samples[].r_m must hold 3 numbers")
    if any(x.shape != (3,) for x in v):
        raise ValueError("samples[].v_mps must hold 3 numbers")
    return np.array(times), np.array(r), np.array(v)


def _field(model: Any, name: str, label: str, default: Any = _MISSING) -> Any:
    """Read ``name`` from a mapping or an attribute object; ValueError naming it when absent."""
    if isinstance(model, Mapping):
        value = model.get(name, _MISSING)
    else:
        value = getattr(model, name, _MISSING)
    if value is _MISSING or value is None:
        if default is _MISSING:
            raise ValueError(f"{label} is required")
        return default
    return value


def _epoch_seconds(value: Any, name: str) -> float:
    """ISO string, datetime or float Unix seconds -> float Unix seconds."""
    if isinstance(value, str):
        try:
            return sec(ns_from_iso(value))
        except ValueError as exc:
            raise ValueError(f"{name}: {exc}") from None
    if isinstance(value, _dt.datetime):
        return sec(ns_from_datetime(value))
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, float, np.integer, np.floating)):
        raise ValueError(f"{name} must be an ISO-8601 string, a datetime or Unix seconds")
    seconds = float(value)
    if not np.isfinite(seconds):
        raise ValueError(f"{name} must be finite")
    if abs(seconds) > MAX_UNIX_S:
        raise ValueError(f"{name}={value!r} is not Unix seconds (nanoseconds are not accepted here)")
    return seconds


def _finite(value: Any, name: str) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number, got {value!r}") from None
    if not np.isfinite(out):
        raise ValueError(f"{name} must be finite, got {value!r}")
    return out


def _vector3(value: Any, name: str) -> np.ndarray:
    arr = np.asarray(value, dtype=np.float64)
    if arr.shape != (3,) or not np.all(np.isfinite(arr)):
        raise ValueError(f"{name} must be 3 finite numbers, got shape {arr.shape}")
    return arr


def _times(t_s: Any, dt_s: Any = 0.0) -> tuple[np.ndarray, np.ndarray]:
    """Time and offset as float64 arrays broadcast to a common shape; rejects non-finite values."""
    t = np.asarray(t_s, dtype=np.float64)
    extra = np.asarray(dt_s, dtype=np.float64)
    if not np.all(np.isfinite(t)):
        raise ValueError("t_s must be finite")
    if not np.all(np.isfinite(extra)):
        raise ValueError("dt_s must be finite")
    if t.size and float(np.max(np.abs(t))) > MAX_UNIX_S:
        raise ValueError(
            f"t_s must be Unix seconds, got a value of magnitude {float(np.max(np.abs(t))):.3g} "
            "(integer nanoseconds t_ns go through timebase.sec first)"
        )
    if t.shape != extra.shape:
        t, extra = np.broadcast_arrays(t, extra)
    return t, extra


def _rot_z(vec: np.ndarray, angle: np.ndarray) -> np.ndarray:
    """Rotate vectors ``[..., 3]`` counter-clockwise about +z by ``angle`` (``[...]``, rad)."""
    c, s = np.cos(angle), np.sin(angle)
    x, y = vec[..., 0], vec[..., 1]
    return np.stack([x * c - y * s, x * s + y * c, vec[..., 2]], axis=-1)


def _omega_cross(rate: float, r: np.ndarray) -> np.ndarray:
    """``[0, 0, rate] x r`` for vectors ``[..., 3]``."""
    return np.stack([-rate * r[..., 1], rate * r[..., 0], np.zeros_like(r[..., 0])], axis=-1)
