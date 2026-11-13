"""timebase: exact integer nanosecond time and the linear Earth rotation angle."""
from __future__ import annotations

import calendar
import datetime as dt
import time
from fractions import Fraction

import numpy as np
import pytest
from sgp4.propagation import gstime

from petal_locate.core import timebase as tb
from petal_locate.core.constants import GMST_REF, JD_UNIX_EPOCH, OMEGA_GMST, T_REF_S

NS = 1_000_000_000


# ----------------------------------------------------------------------------- ns_from_iso


def test_unix_epoch_and_reference_epoch() -> None:
    assert tb.ns_from_iso("1970-01-01T00:00:00Z") == 0
    assert tb.ns_from_iso("2026-01-01T00:00:00Z") == int(T_REF_S) * NS
    # independent of the parser: the C library's calendar arithmetic
    assert tb.ns_from_iso("2026-11-13T07:09:31Z") == calendar.timegm((2026, 11, 13, 7, 9, 31)) * NS


@pytest.mark.parametrize(
    "text, expected_ns",
    [
        ("2026-03-04T05:06:07.123456789Z", calendar.timegm((2026, 3, 4, 5, 6, 7)) * NS + 123_456_789),
        ("2026-03-04T05:06:07.000000001Z", calendar.timegm((2026, 3, 4, 5, 6, 7)) * NS + 1),
        ("2026-03-04T05:06:07.5Z", calendar.timegm((2026, 3, 4, 5, 6, 7)) * NS + 500_000_000),
        ("2026-03-04T05:06:07,25Z", calendar.timegm((2026, 3, 4, 5, 6, 7)) * NS + 250_000_000),
        ("2026-03-04 05:06:07z", calendar.timegm((2026, 3, 4, 5, 6, 7)) * NS),
        ("2026-03-04T05:06:07", calendar.timegm((2026, 3, 4, 5, 6, 7)) * NS),  # naive = UTC
        ("2026-03-04T05:06Z", calendar.timegm((2026, 3, 4, 5, 6, 0)) * NS),
        ("2026-03-04", calendar.timegm((2026, 3, 4, 0, 0, 0)) * NS),
        ("2024-02-29T23:59:59.999999999Z", calendar.timegm((2024, 2, 29, 23, 59, 59)) * NS + 999_999_999),
        ("1969-12-31T23:59:59.999999999Z", -1),
        ("1900-01-01T00:00:00Z", calendar.timegm((1900, 1, 1, 0, 0, 0)) * NS),
    ],
)
def test_parse_known_values(text: str, expected_ns: int) -> None:
    out = tb.ns_from_iso(text)
    assert isinstance(out, int)
    assert out == expected_ns


@pytest.mark.parametrize(
    "text, utc",
    [
        ("2026-03-04T14:06:07+09:00", (2026, 3, 4, 5, 6, 7)),
        ("2026-03-04T14:06:07+0900", (2026, 3, 4, 5, 6, 7)),
        ("2026-03-04T14:06:07+09", (2026, 3, 4, 5, 6, 7)),
        ("2026-03-03T23:36:07-05:30", (2026, 3, 4, 5, 6, 7)),
        ("2026-03-04T05:06:07+00:00", (2026, 3, 4, 5, 6, 7)),
        ("2026-01-01T00:30:00+01:00", (2025, 12, 31, 23, 30, 0)),  # crosses a year boundary
    ],
)
def test_explicit_offsets(text: str, utc: tuple[int, ...]) -> None:
    assert tb.ns_from_iso(text) == calendar.timegm(utc) * NS
    # and the standard library agrees on the offset convention
    assert tb.ns_from_iso(text) == int(dt.datetime.fromisoformat(text).timestamp()) * NS


def test_fraction_is_never_parsed_through_a_float() -> None:
    # float64 cannot hold 1.7e18 + 1: any float path would lose the last nanosecond
    base = calendar.timegm((2026, 11, 13, 7, 9, 0)) * NS
    for frac in (1, 7, 123_456_789, 999_999_999, 100_000_001):
        text = f"2026-11-13T07:09:00.{frac:09d}Z"
        assert tb.ns_from_iso(text) == base + frac
        assert float(base + frac) != base + frac or frac % 256 == 0  # the float really is lossy


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "not a time",
        "2026-13-01T00:00:00Z",
        "2026-02-30T00:00:00Z",
        "2026-01-01T24:00:00Z",
        "2026-01-01T00:60:00Z",
        "2026-12-31T23:59:60Z",  # leap second: not representable in POSIX time
        "2026-01-01T00:00:00.1234567891Z",  # 10 fractional digits
        "2026-01-01T00:00:00+25:00",
        "2026-01-01T00:00:00+01:75",
        "20260101T000000Z",
        "2026-01-01T00:00:00 UTC",
        "2026-1-1T00:00:00Z",
    ],
)
def test_invalid_strings_raise(bad: str) -> None:
    with pytest.raises(ValueError, match="s "):
        tb.ns_from_iso(bad)


def test_non_string_raises() -> None:
    with pytest.raises(ValueError, match="s must be"):
        tb.ns_from_iso(1767225600)  # type: ignore[arg-type]


# ----------------------------------------------------------------------------- iso_from_ns


@pytest.mark.parametrize(
    "t_ns, text",
    [
        (0, "1970-01-01T00:00:00Z"),
        (int(T_REF_S) * NS, "2026-01-01T00:00:00Z"),
        (int(T_REF_S) * NS + 500_000_000, "2026-01-01T00:00:00.500Z"),
        (int(T_REF_S) * NS + 123_456_000, "2026-01-01T00:00:00.123456Z"),
        (int(T_REF_S) * NS + 1, "2026-01-01T00:00:00.000000001Z"),
        (-1, "1969-12-31T23:59:59.999999999Z"),
        (86400 * NS - 1_000_000, "1970-01-01T23:59:59.999Z"),
    ],
)
def test_format_known_values(t_ns: int, text: str) -> None:
    assert tb.iso_from_ns(t_ns) == text
    assert tb.ns_from_iso(text) == t_ns


def test_round_trip_random_instants() -> None:
    rng = np.random.default_rng(20261113)
    lo = calendar.timegm((1901, 1, 1, 0, 0, 0)) * NS
    hi = calendar.timegm((2199, 1, 1, 0, 0, 0)) * NS
    for _ in range(2000):
        t_ns = int(rng.integers(lo // NS, hi // NS)) * NS + int(rng.integers(0, NS))
        assert tb.ns_from_iso(tb.iso_from_ns(t_ns)) == t_ns


def test_format_agrees_with_datetime_at_microsecond_resolution() -> None:
    rng = np.random.default_rng(7)
    for _ in range(200):
        micro = int(rng.integers(0, 4_000_000_000_000_000))
        t_ns = micro * 1000
        ref = dt.datetime(1970, 1, 1) + dt.timedelta(microseconds=micro)
        assert tb.ns_from_iso(tb.iso_from_ns(t_ns)) == t_ns
        assert tb.iso_from_ns(t_ns)[:19] == ref.strftime("%Y-%m-%dT%H:%M:%S")
        assert tb.ns_from_datetime(ref) == t_ns


def test_iso_from_ns_rejects_floats_and_out_of_range() -> None:
    with pytest.raises(ValueError, match="t_ns"):
        tb.iso_from_ns(1.5e18)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="t_ns"):
        tb.iso_from_ns(10**30)
    assert tb.iso_from_ns(np.int64(NS)) == "1970-01-01T00:00:01Z"


def test_ns_from_datetime_handles_time_zones() -> None:
    naive = dt.datetime(2026, 3, 4, 5, 6, 7, 123456)
    aware = naive.replace(tzinfo=dt.timezone.utc)
    tokyo = aware.astimezone(dt.timezone(dt.timedelta(hours=9)))
    expected = calendar.timegm((2026, 3, 4, 5, 6, 7)) * NS + 123_456_000
    assert tb.ns_from_datetime(naive) == expected
    assert tb.ns_from_datetime(aware) == expected
    assert tb.ns_from_datetime(tokyo) == expected
    with pytest.raises(ValueError, match="datetime"):
        tb.ns_from_datetime("2026-03-04")  # type: ignore[arg-type]


# ----------------------------------------------------------------------------- sec, now_ns


def test_sec_is_exact_for_whole_seconds_and_correctly_rounded() -> None:
    assert tb.sec(tb.ns_from_iso("2026-01-01T00:00:00Z")) == T_REF_S
    assert tb.sec(0) == 0.0
    rng = np.random.default_rng(3)
    for _ in range(500):
        t_ns = int(rng.integers(0, 2 * 10**18))
        # exact rational comparison: the float is the nearest one to t_ns / 1e9
        value = tb.sec(t_ns)
        err = abs(Fraction(value) - Fraction(t_ns, NS))
        assert err <= Fraction(np.spacing(value)) / 2
    with pytest.raises(ValueError, match="t_ns"):
        tb.sec(1.7e18)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="t_ns"):
        tb.sec(True)


def test_now_ns_tracks_the_system_clock() -> None:
    before = time.time_ns()
    now = tb.now_ns()
    after = time.time_ns()
    assert isinstance(now, int)
    assert before <= now <= after


# ----------------------------------------------------------------------------- gmst_rad


def test_gmst_reference_value_is_sgp4_gstime_at_the_reference_epoch() -> None:
    assert GMST_REF == gstime(JD_UNIX_EPOCH + T_REF_S / 86400.0)
    assert tb.gmst_rad(T_REF_S) == pytest.approx(GMST_REF, abs=1e-15)


def test_gmst_matches_sgp4_gstime_from_2024_to_2031() -> None:
    t = np.linspace(tb.sec(tb.ns_from_iso("2024-01-01T00:00:00Z")), tb.sec(tb.ns_from_iso("2031-01-01T00:00:00Z")), 3001)
    theta = tb.gmst_rad(t)
    ref = np.array([gstime(JD_UNIX_EPOCH + ti / 86400.0) for ti in t])
    diff = np.mod(theta - ref + np.pi, 2 * np.pi) - np.pi
    assert np.max(np.abs(diff)) < 5e-8
    assert np.all((theta >= 0.0) & (theta < 2 * np.pi))


def test_gmst_equals_the_linear_formula_evaluated_exactly() -> None:
    """Against exact rational arithmetic on the float64 constants of MATH.md section 1."""
    two_pi = Fraction(2 * np.pi)
    rng = np.random.default_rng(11)
    t = rng.uniform(1.70e9, 1.93e9, 300)
    for ti, dut1 in zip(t, rng.uniform(-0.9, 0.9, t.size)):
        exact = Fraction(GMST_REF) + Fraction(OMEGA_GMST) * ((Fraction(ti) - Fraction(T_REF_S)) + Fraction(dut1))
        exact -= two_pi * (exact // two_pi)
        got = Fraction(float(tb.gmst_rad(ti, dut1)))
        err = abs(got - exact)
        err = min(err, two_pi - err)
        assert float(err) < 3e-15


def test_gmst_dut1_is_a_time_shift_and_the_rate_is_omega_gmst() -> None:
    t = 1.79e9 + np.array([0.0, 1234.5, 86400.0])
    shifted = tb.gmst_rad(t + 0.25)
    with_dut1 = tb.gmst_rad(t, 0.25)
    assert np.max(np.abs(np.mod(shifted - with_dut1 + np.pi, 2 * np.pi) - np.pi)) < 1e-12
    step = np.mod(tb.gmst_rad(t + 64.0) - tb.gmst_rad(t), 2 * np.pi)
    assert np.allclose(step, OMEGA_GMST * 64.0, rtol=0, atol=1e-13)


def test_gmst_is_smooth_sample_to_sample() -> None:
    """No last-bit jitter: 0.5 s at 31.25 kHz deviates from a straight line by < 5e-15 rad.

    The naive product OMEGA_GMST * (t - T_REF_S) is thousands of radians and jitters by
    about 1e-12 rad, which is 4e-5 m of satellite position at GEO.
    """
    for t0 in (1.7945e9, 1.90e9):
        t = t0 + np.arange(15625) / 31250.0
        theta = np.unwrap(tb.gmst_rad(t))
        x = t - t[0]
        resid = theta - theta[0] - OMEGA_GMST * x
        resid -= resid.mean()
        assert np.max(np.abs(resid)) < 5e-15
        naive = np.unwrap(np.mod(GMST_REF + OMEGA_GMST * (t - T_REF_S), 2 * np.pi))
        naive_resid = naive - naive[0] - OMEGA_GMST * x
        assert np.max(np.abs(naive_resid - naive_resid.mean())) > 20 * np.max(np.abs(resid))


def test_gmst_offset_argument_carries_sub_resolution_time() -> None:
    """The second argument may be an array: an offset far below the 2.4e-7 s spacing of
    float64 Unix times still turns the Earth by OMEGA_GMST * offset."""
    t = np.full(5, 1.7945e9)
    offsets = np.array([-2e-8, -1e-9, 0.0, 1e-9, 2e-8])
    assert np.all(t + offsets == t)
    theta = tb.gmst_rad(t, offsets)
    assert np.allclose(theta - theta[2], OMEGA_GMST * offsets, rtol=0, atol=1e-15)
    assert np.all(np.diff(theta) > 0.0)
    assert tb.gmst_rad(t[:, None], offsets).shape == (5, 5)


def test_gmst_shapes() -> None:
    assert np.ndim(tb.gmst_rad(1.79e9)) == 0
    assert tb.gmst_rad(np.full((2, 3), 1.79e9)).shape == (2, 3)


def test_wrapped_phase_matches_exact_arithmetic() -> None:
    two_pi = Fraction(2 * np.pi)
    rng = np.random.default_rng(5)
    for rate, dt_s in zip(rng.uniform(1e-5, 2e-3, 200), rng.uniform(-3e8, 3e8, 200)):
        exact = Fraction(rate) * Fraction(dt_s)
        got = Fraction(float(tb.wrapped_phase(rate, dt_s)))
        k = round((exact - got) / two_pi)
        assert abs(float(exact - got - k * two_pi)) < 2e-15
        assert abs(float(got)) < 2 * np.pi
