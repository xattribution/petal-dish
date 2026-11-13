"""Time conventions (MATH.md section 1).

Absolute time is ``t_ns``: integer UTC nanoseconds since the Unix epoch (POSIX, no leap
seconds), a Python ``int`` end to end. Float seconds (``sec(t_ns)``) are for evaluating
orbits and geometry only; a float64 near 1.7e9 s resolves 240 ns.

Nothing here keeps state; ``now_ns`` is the only function that reads the clock.
"""
from __future__ import annotations

import datetime as _dt
import operator
import re
import time
from typing import Any

import numpy as np

from .constants import GMST_REF, OMEGA_GMST, T_REF_S

__all__ = [
    "MAX_UNIX_S",
    "now_ns",
    "ns_from_iso",
    "ns_from_datetime",
    "iso_from_ns",
    "sec",
    "gmst_rad",
    "wrapped_phase",
]

MAX_UNIX_S: float = 1.0e11
"""Largest |t_s| accepted as float Unix seconds (year 5138). A 2026 timestamp in
milliseconds is 1.8e12 and in nanoseconds 1.8e18, so an integer ``t_ns`` handed to a
seconds argument is rejected (ValueError) by the orbit, geometry and Earth-rotation
routines instead of producing a plausible-looking angle or position."""

_NS_PER_S = 1_000_000_000
_UNIX_ORDINAL = _dt.date(1970, 1, 1).toordinal()
_TWO_PI = 2.0 * np.pi

_ISO_RE = re.compile(
    r"""^\s*
    (?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})
    (?:[Tt\ ]
       (?P<hour>\d{2}):(?P<minute>\d{2})
       (?::(?P<second>\d{2})(?:[.,](?P<frac>\d+))?)?
    )?
    \s*(?P<zone>[Zz]|[+-]\d{2}(?::?\d{2})?)?
    \s*$""",
    re.VERBOSE,
)


def now_ns() -> int:
    """Current UTC time, integer nanoseconds since the Unix epoch."""
    return time.time_ns()


def ns_from_iso(s: str) -> int:
    """Parse an ISO-8601 timestamp to integer UTC nanoseconds since the Unix epoch.

    Accepted: ``YYYY-MM-DD[THH:MM[:SS[.fffffffff]]][Z|+HH[:MM]|-HH[:MM]]`` (``T`` may be
    a space; up to 9 fractional digits). A trailing ``Z`` or no designator means UTC;
    an explicit offset is local-minus-UTC and is subtracted. The result is exact to the
    nanosecond: the fraction is parsed as an integer, never through a float.

    Raises ValueError (naming ``s``) for anything else, including a leap second
    (``:60``), which POSIX time cannot represent.
    """
    if not isinstance(s, str):
        raise ValueError(f"s must be an ISO-8601 string, got {type(s).__name__}")
    m = _ISO_RE.match(s)
    if m is None:
        raise ValueError(f"s is not an ISO-8601 timestamp: {s!r}")
    year, month, day = int(m["year"]), int(m["month"]), int(m["day"])
    hour = int(m["hour"] or 0)
    minute = int(m["minute"] or 0)
    second = int(m["second"] or 0)
    frac = m["frac"] or ""
    if len(frac) > 9:
        raise ValueError(f"s has more than 9 fractional-second digits: {s!r}")
    if hour > 23 or minute > 59 or second > 59:
        raise ValueError(f"s has a time of day out of range (POSIX, no leap seconds): {s!r}")
    try:
        days = _dt.date(year, month, day).toordinal() - _UNIX_ORDINAL
    except ValueError as exc:
        raise ValueError(f"s has an invalid calendar date: {s!r} ({exc})") from None
    offset_s = 0
    zone = m["zone"]
    if zone and zone not in "Zz":
        digits = zone[1:].replace(":", "")
        off_h, off_m = int(digits[:2]), int(digits[2:] or 0)
        if off_h > 23 or off_m > 59:
            raise ValueError(f"s has a UTC offset out of range: {s!r}")
        offset_s = (off_h * 3600 + off_m * 60) * (-1 if zone[0] == "-" else 1)
    frac_ns = int(frac.ljust(9, "0")) if frac else 0
    return (days * 86400 + hour * 3600 + minute * 60 + second - offset_s) * _NS_PER_S + frac_ns


def ns_from_datetime(value: _dt.datetime) -> int:
    """Exact integer UTC nanoseconds since the Unix epoch of a ``datetime``.

    A naive datetime is taken as UTC; an aware one is converted. Integer arithmetic
    throughout (datetime resolution is one microsecond).
    """
    if not isinstance(value, _dt.datetime):
        raise ValueError(f"value must be a datetime, got {type(value).__name__}")
    if value.tzinfo is not None:
        offset = value.utcoffset()
        if offset is not None:
            value = (value - offset).replace(tzinfo=None)
        else:
            value = value.replace(tzinfo=None)
    delta = value - _dt.datetime(1970, 1, 1)
    return (delta.days * 86400 + delta.seconds) * _NS_PER_S + delta.microseconds * 1000


def iso_from_ns(t_ns: int) -> str:
    """Format integer UTC nanoseconds since the Unix epoch as ISO-8601 with a ``Z``.

    ``YYYY-MM-DDTHH:MM:SSZ`` when the time is a whole second, otherwise with 3, 6 or 9
    fractional digits (the fewest that are exact). ``ns_from_iso(iso_from_ns(t)) == t``
    for every representable ``t`` (years 0001 to 9999).
    """
    t = _as_int(t_ns, "t_ns")
    secs, frac = divmod(t, _NS_PER_S)
    days, sod = divmod(secs, 86400)
    try:
        date = _dt.date.fromordinal(days + _UNIX_ORDINAL)
    except (ValueError, OverflowError):
        raise ValueError(f"t_ns is outside years 0001..9999: {t_ns!r}") from None
    hour, rem = divmod(sod, 3600)
    minute, second = divmod(rem, 60)
    if frac == 0:
        frac_txt = ""
    elif frac % 1_000_000 == 0:
        frac_txt = f".{frac // 1_000_000:03d}"
    elif frac % 1_000 == 0:
        frac_txt = f".{frac // 1_000:06d}"
    else:
        frac_txt = f".{frac:09d}"
    return (
        f"{date.year:04d}-{date.month:02d}-{date.day:02d}"
        f"T{hour:02d}:{minute:02d}:{second:02d}{frac_txt}Z"
    )


def sec(t_ns: int) -> float:
    """Float Unix seconds of ``t_ns`` (integer nanoseconds), correctly rounded.

    For orbit and geometry evaluation only: the result resolves about 240 ns. Whole
    seconds convert exactly. Never multiply a frequency by this value.
    """
    return _as_int(t_ns, "t_ns") / _NS_PER_S


def wrapped_phase(rate_rad_s: float, dt_s: Any) -> Any:
    """``(rate_rad_s * dt_s) mod 2 pi`` without the rounding of the large product.

    rate_rad_s: angular rate, rad/s (scalar). dt_s: elapsed seconds, scalar or array.
    Returns radians in ``(-2 pi, 2 pi)`` with the sign of the product.

    A plain ``rate * dt`` reaches thousands of radians a year from the epoch, so its
    last-bit rounding is 1e-12 rad of sample-to-sample jitter (4e-5 m at GEO). Here the
    product is formed exactly as a double-double (Dekker), reduced with the exact
    ``fmod`` and only then rounded, leaving about 1e-15 rad.
    """
    dt = np.asarray(dt_s, dtype=np.float64)
    rate = float(rate_rad_s)
    p = rate * dt
    r_hi, r_lo = _split(np.float64(rate))
    d_hi, d_lo = _split(dt)
    err = ((r_hi * d_hi - p) + r_hi * d_lo + r_lo * d_hi) + r_lo * d_lo
    out = np.fmod(p, _TWO_PI) + err
    return out[()] if out.ndim == 0 else out


def gmst_rad(t_s: Any, dut1_s: Any = 0.0) -> Any:
    """Earth rotation angle for inertial orbits, radians in ``[0, 2 pi)``.

    t_s: Unix seconds (UTC), scalar or array. dut1_s: UT1 - UTC, seconds (a scalar, or
    an array broadcast against ``t_s``: any small time offset can ride here and is
    applied without being rounded into the large ``t_s``).
    ``theta = (GMST_REF + OMEGA_GMST * ((t_s - T_REF_S) + dut1_s)) mod 2 pi``
    (MATH.md section 1): linear in time, within 5e-8 rad of ``sgp4``'s ``gstime`` from
    2024 to 2031 and smooth to about 1e-15 rad (see ``wrapped_phase``). A ``t_s`` beyond
    ``MAX_UNIX_S`` (nanoseconds passed as seconds) raises ValueError.
    """
    t = np.asarray(t_s, dtype=np.float64)
    offset = np.asarray(dut1_s, dtype=np.float64)
    if t.size and float(np.max(np.abs(t))) > MAX_UNIX_S:
        raise ValueError(
            f"t_s must be Unix seconds, got a value of magnitude {float(np.max(np.abs(t))):.3g} "
            "(integer nanoseconds t_ns go through sec() first)"
        )
    theta = (GMST_REF + OMEGA_GMST * offset) + wrapped_phase(OMEGA_GMST, t - T_REF_S)
    theta = np.mod(theta, _TWO_PI)
    # np.mod can return exactly 2 pi for a tiny negative argument; keep the half-open range.
    theta = np.where(theta >= _TWO_PI, 0.0, theta)
    return theta[()] if theta.ndim == 0 else theta


def _split(a: Any) -> tuple[Any, Any]:
    """Veltkamp split of a float64 into high and low 26-bit halves (a = hi + lo exactly)."""
    c = 134217729.0 * a  # 2**27 + 1
    hi = c - (c - a)
    return hi, a - hi


def _as_int(value: Any, name: str) -> int:
    """Return ``value`` as a Python int, rejecting floats and bools (ValueError naming it)."""
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer number of nanoseconds, got bool")
    try:
        return operator.index(value)
    except TypeError:
        raise ValueError(
            f"{name} must be an integer number of nanoseconds, got {type(value).__name__}"
        ) from None
