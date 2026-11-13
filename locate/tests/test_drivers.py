"""Radio drivers: the interface and its helpers, the registry, noise, SigMF and SoapySDR.

Numeric helpers are checked against independent calculations (exact rational
arithmetic, closed-form tones, filter energy). The SoapySDR driver has never met a
radio: what is tested here is its pure machinery (ring buffer, stream choice, PPS
arithmetic), its failure when the library is missing, and its control flow against a
software stand-in for the SoapySDR module. None of that is evidence about hardware.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import importlib.util
import json
import sys
import threading
import time
import types
from fractions import Fraction
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from petal_locate.core.spectrum import welch_psd_db
from petal_locate.node.clock import ManualClock
from petal_locate.node.drivers import (
    DRIVER_NAMES,
    driver_names,
    get_driver,
    register_driver,
)
from petal_locate.node.drivers.base import (
    Capabilities,
    Capture,
    CaptureError,
    DriverContext,
    WorkerPool,
    duration_ns,
    mixer_phase_cycles,
    plan_resample,
    retune_resample,
    sample_index,
    sample_time_ns,
)
from petal_locate.node.drivers.noise import NoiseDriver, noise_samples
from petal_locate.node.drivers.sigmf import (
    SigMFDriver,
    load_recording,
    ns_from_sigmf_datetime,
    recording_slice,
)
from petal_locate.node.drivers.soapy import (
    SampleRing,
    SoapyDriver,
    choose_stream,
    next_pps_time_ns,
)
from petal_locate.node.drivers import soapy as soapy_module
from petal_locate.protocol import Capabilities as WireCapabilities
from petal_locate.protocol import ChannelAssignment, MonitorAssignment

NS = 1_000_000_000
T0 = 1_800_000_000 * NS


def channel(channel_id: str = "ch-a", *monitors: MonitorAssignment) -> ChannelAssignment:
    return ChannelAssignment(channel_id=channel_id, antenna_summary="test", monitors=list(monitors))


def context(clock: Any = None, workers: WorkerPool | None = None) -> DriverContext:
    return DriverContext(
        site_id="site-a", server_http_url="http://127.0.0.1:1", token="tok", simulation=None,
        clock=clock, workers=workers,
    )


async def settle(turns: int = 5) -> None:
    for _ in range(turns):
        await asyncio.sleep(0)


async def finish(task: asyncio.Task[Any], timeout: float = 2.0) -> Any:
    return await asyncio.wait_for(task, timeout)


# ============================================================================= base


def test_capabilities_are_validated_and_convert_to_the_wire_model() -> None:
    caps = Capabilities(100e6, 400e6, 2e6, 1.5, 3)
    assert caps.to_wire() == WireCapabilities(
        min_hz=100e6, max_hz=400e6, max_sample_rate_hz=2e6, lookback_s=1.5, concurrent_reads=3
    )
    assert Capabilities(0, 1, 1, 0, 1).to_wire().min_hz == 0.0  # ints are accepted
    for field_name, kwargs in (
        ("max_hz", dict(min_hz=5.0, max_hz=4.0)),
        ("min_hz", dict(min_hz=-1.0)),
        ("max_sample_rate_hz", dict(max_sample_rate_hz=0.0)),
        ("max_sample_rate_hz", dict(max_sample_rate_hz=float("inf"))),
        ("lookback_s", dict(lookback_s=float("nan"))),
        ("concurrent_reads", dict(concurrent_reads=0)),
        ("concurrent_reads", dict(concurrent_reads=1.5)),
    ):
        values: dict[str, Any] = dict(
            min_hz=1.0, max_hz=2.0, max_sample_rate_hz=1.0, lookback_s=0.0, concurrent_reads=1
        )
        values.update(kwargs)
        with pytest.raises(ValueError, match=field_name):
            Capabilities(**values)


def test_capture_error_carries_code_and_message_and_refuses_unknown_codes() -> None:
    error = CaptureError("too_late", "interval is gone")
    assert (error.code, error.message, str(error)) == (
        "too_late", "interval is gone", "too_late: interval is gone",
    )
    assert str(CaptureError("busy")) == "busy"
    for code in ("out_of_range", "too_late", "overflow", "driver", "busy", "cancelled"):
        assert CaptureError(code, "m").code == code
    with pytest.raises(ValueError, match="unknown capture error code 'late'"):
        CaptureError("late", "typo")


def test_driver_context_keeps_the_token_out_of_its_repr() -> None:
    ctx = DriverContext("site", "https://server:8443", "very-secret", {"enabled": True})
    assert "very-secret" not in repr(ctx) and ctx.token == "very-secret"
    assert ctx.clock is None and ctx.workers is None  # the optional extensions
    capture = Capture(np.zeros(1, np.complex64), 5, 1.0, 2.0)
    assert (capture.t_first_ns, capture.fs, capture.fc) == (5, 1.0, 2.0)
    assert capture.overflow is False and capture.clock_locked is True  # the defaults


def test_sample_time_and_index_are_exact_integers_at_any_distance() -> None:
    # 1 MS/s: one sample is exactly 1000 ns, also 10^15 samples (31 years) out, where
    # float64 seconds would have lost the nanoseconds
    far = 10**15 + 7
    assert sample_time_ns(far, T0, 1e6) == T0 + far * 1000
    assert sample_index(T0 + far * 1000, T0, 1e6) == far
    assert sample_index(T0 + far * 1000 + 499, T0, 1e6) == far  # nearest sample
    assert sample_index(T0 + far * 1000 + 500, T0, 1e6) == far + 1  # halves go up
    assert sample_index(T0 - 1500, T0, 1e6) == -1  # -1.5 rounds half up to -1
    assert sample_index(T0 - 1501, T0, 1e6) == -2
    assert sample_time_ns(-3, T0, 1e6) == T0 - 3000
    # 48 kHz: the period is not a whole number of ns, yet a year of samples is exact
    year = 365 * 86400
    assert sample_time_ns(48_000 * year, T0, 48_000.0) == T0 + year * NS
    # independent rational check on awkward rates and random indices
    rng = np.random.default_rng(5)
    for fs in (48_000.0, 1e6 / 3, 2.4e6, 37_123.456):
        for k in (int(v) for v in rng.integers(0, 2**52, size=20)):
            exact = Fraction(k * NS) / Fraction(fs)
            t = sample_time_ns(k, T0, fs)
            assert abs(Fraction(t - T0) - exact) <= Fraction(1, 2)
            assert sample_index(t, T0, fs) == k or Fraction(fs) > NS  # round trip
    assert duration_ns(1, 3.0) == 333_333_334 and duration_ns(48_000, 48_000.0) == NS
    assert duration_ns(0, 1e6) == 0
    for call in (lambda: sample_index(0, 0, 0.0), lambda: sample_time_ns(1, 0, float("nan")),
                 lambda: duration_ns(-1, 1.0)):
        with pytest.raises(ValueError):
            call()


def test_mixer_phase_is_exact_where_floating_point_is_not() -> None:
    # 12.5 kHz at 1 MS/s advances 1/80 cycle per sample; at index 10^12 + 3 the phase
    # is 3/80 of a cycle exactly. shift * index / fs in float64 cannot resolve that.
    assert mixer_phase_cycles(12_500.0, 10**12 + 3, 1e6) == pytest.approx(3 / 80, abs=1e-15)
    assert mixer_phase_cycles(-12_500.0, 10**12 + 3, 1e6) == pytest.approx(1 - 3 / 80, abs=1e-15)
    assert mixer_phase_cycles(0.0, 99, 1e6) == 0.0
    for shift, index, fs in ((200e3, 123_456_789_012, 1e6), (33_333.0, 7**20, 2.4e6)):
        exact = (Fraction(shift) * index / Fraction(fs)) % 1
        assert mixer_phase_cycles(shift, index, fs) == pytest.approx(float(exact), abs=1e-15)


def test_plan_resample_ratios_margins_and_reported_rate() -> None:
    plan = plan_resample(1e6, 435e6, 250e3, 435.2e6)
    assert (plan.up, plan.down, plan.shift_hz, plan.fs_out, plan.fc_out) == (
        1, 4, pytest.approx(200e3), 250e3, 435.2e6,
    )
    assert plan.margin % plan.down == 0 and plan.margin >= 40  # ten output samples a side
    assert plan.core_length(1000) == 4000 and plan.input_length(1000) == 4000 + 2 * plan.margin
    assert not plan.identity
    assert plan_resample(1e6, 435e6, 1e6, 435e6).identity
    assert plan_resample(1e6, 435e6, 1e6, 435e6).margin == 0
    up_down = [(p.up, p.down) for p in (
        plan_resample(1e6, 0.5e6, 400e3, 0.5e6),
        plan_resample(1e6, 0.5e6, 48e3, 0.5e6),
        plan_resample(2.4e6, 1.2e6, 1e6, 1.2e6),
    )]
    assert up_down == [(2, 5), (6, 125), (5, 12)]
    assert plan_resample(1e6, 0.5e6, 400e3, 0.5e6).core_length(3) == 8  # ceil(3 * 5 / 2)
    # a rate that is a simple fraction only approximately is produced and reported as asked
    third = plan_resample(1e6, 5e6, 1e6 / 3, 5e6)
    assert (third.up, third.down, third.fs_out) == (1, 3, 1e6 / 3)
    # one that needs huge terms is approximated, and the plan says what really comes out
    odd = plan_resample(1e6, 5e6, 123_456.789, 5e6)
    assert odd.down <= 4096 and odd.fs_out == pytest.approx(123_456.789, rel=1e-6)
    assert odd.fs_out == float(Fraction(1e6) * Fraction(odd.up, odd.down)) != 123_456.789


def test_plan_resample_refuses_bands_outside_the_source() -> None:
    for fs_out, fc_out in (
        (2e6, 435e6),  # faster than the source
        (250e3, 435.4e6),  # upper edge at +525 kHz
        (250e3, 434.6e6),  # lower edge at -525 kHz
        (1e6, 435.001e6),  # full rate, shifted
        (float("nan"), 435e6),
        (0.0, 435e6),
        (250e3, float("inf")),
        (10.0, 435e6),  # 100000 times slower: no usable ratio
    ):
        with pytest.raises(CaptureError) as caught:
            plan_resample(1e6, 435e6, fs_out, fc_out)
        assert caught.value.code == "out_of_range"
    plan_resample(1e6, 435e6, 250e3, 435.375e6)  # exactly at the edge is inside
    with pytest.raises(ValueError, match="fs_in"):
        plan_resample(0.0, 1.0, 1.0, 1.0)


@pytest.mark.parametrize(
    ("fs_out", "fc_out", "f_rf", "tolerance"),
    [
        (250e3, 435.2e6, 435.23e6, 1.5e-3),  # 1/4, tone at +30 kHz of the new centre
        (250e3, 435.2e6, 435.30e6, 5e-3),  # at 80 % of the new band edge
        (400e3, 434.9e6, 434.95e6, 2e-3),  # 2/5
        (48e3, 435.3e6, 435.31e6, 1e-3),  # 6/125
        (1e6 / 3, 435.0e6, 435.1e6, 1e-3),  # 1/3, no retune
    ],
)
def test_retune_resample_reproduces_a_tone_in_frequency_phase_and_time(
    fs_out: float, fc_out: float, f_rf: float, tolerance: float
) -> None:
    """A tone at RF frequency f_rf, recorded at (fs_in, fc_in), must come out as the
    same tone seen from (fs_out, fc_out), sample for sample: this pins the sign of the
    mixer, its phase reference, the resampling ratio and the alignment of output
    sample 0 with the first source sample (a slip of one source sample would turn the
    phase by up to 0.6 rad here)."""
    fs_in, fc_in = 1e6, 435e6
    plan = plan_resample(fs_in, fc_in, fs_out, fc_out)
    n_out, first = 4000, 10**9 + 12_345  # first source index: far into a long stream
    count = plan.input_length(n_out)
    offset_in = Fraction(f_rf) - Fraction(fc_in)  # tone frequency in the source, Hz
    start = first - plan.margin
    start_phase = float((offset_in * start / Fraction(fs_in)) % 1)
    source = np.exp(
        2j * np.pi * (start_phase + float(offset_in) * np.arange(count) / fs_in)
    ).astype(np.complex64)
    out = retune_resample(source, plan, n_out, mixer_phase_cycles(plan.shift_hz, first, fs_in))
    assert out.dtype == np.complex64 and out.shape == (n_out,)
    # closed form: phase of the source tone at `first`, minus the mixer's, then the
    # tone advancing at (f_rf - fc_out) per output sample
    phase0 = float(((offset_in - Fraction(plan.shift_hz)) * first / Fraction(fs_in)) % 1)
    expected = np.exp(2j * np.pi * (phase0 + (f_rf - fc_out) * np.arange(n_out) / plan.fs_out))
    assert np.max(np.abs(out - expected)) < tolerance


def test_retune_resample_rejects_what_lies_outside_the_new_band() -> None:
    plan = plan_resample(1e6, 435e6, 250e3, 435.2e6)
    count = plan.input_length(2000)
    k = np.arange(count)
    outside = np.exp(2j * np.pi * (-400e3) * k / 1e6).astype(np.complex64)  # 600 kHz away
    out = retune_resample(outside, plan, 2000)
    assert 10 * np.log10(np.mean(np.abs(out) ** 2)) < -50


def test_retune_resample_keeps_the_noise_density() -> None:
    from scipy.signal import firwin

    rng = np.random.default_rng(11)
    plan = plan_resample(1e6, 5e6, 250e3, 5e6)
    n_out = 100_000
    count = plan.input_length(n_out)
    white = ((rng.standard_normal(count) + 1j * rng.standard_normal(count)) / np.sqrt(2)).astype(
        np.complex64
    )
    variance = float(np.mean(np.abs(retune_resample(white, plan, n_out)) ** 2))
    # white noise of variance 1 through a FIR h, then every 4th sample: variance sum(h^2)
    taps = firwin(2 * 10 * 4 + 1, 1 / 4, window=("kaiser", 5.0))
    assert variance == pytest.approx(float(np.sum(taps**2)), rel=0.02)
    assert variance == pytest.approx(1 / 4, rel=0.06)  # the band kept is a quarter


def test_retune_resample_identity_and_argument_checks() -> None:
    plan = plan_resample(1e6, 435e6, 1e6, 435e6)
    source = (np.arange(50) * (1 + 2j)).astype(np.complex64)
    out = retune_resample(source[:20], plan, 20)
    assert out.tobytes() == source[:20].tobytes() and out is not source
    quarter = plan_resample(1e6, 435e6, 250e3, 435e6)
    with pytest.raises(ValueError, match="the plan needs"):
        retune_resample(source, quarter, 10)
    with pytest.raises(ValueError, match="complex"):
        retune_resample(np.zeros(quarter.input_length(10)), quarter, 10)
    with pytest.raises(ValueError, match="n_out"):
        retune_resample(source, quarter, 0)


async def test_worker_pool_runs_in_threads_and_never_exceeds_its_limit() -> None:
    pool = WorkerPool(2)
    lock = threading.Lock()
    state = {"running": 0, "peak": 0}
    loop_thread = threading.get_ident()

    def job(value: int) -> tuple[int, bool]:
        with lock:
            state["running"] += 1
            state["peak"] = max(state["peak"], state["running"])
        time.sleep(0.02)
        with lock:
            state["running"] -= 1
        return value * value, threading.get_ident() != loop_thread

    started = time.monotonic()
    results = await asyncio.gather(*(pool.run(job, i) for i in range(6)))
    assert [r[0] for r in results] == [0, 1, 4, 9, 16, 25] and all(r[1] for r in results)
    assert state["peak"] == 2 and time.monotonic() - started >= 3 * 0.02
    assert pool.active == 0 and pool.limit == 2

    def boom() -> None:
        raise KeyError("from the thread")

    with pytest.raises(KeyError, match="from the thread"):
        await pool.run(boom)
    assert await pool.run(lambda a, b=0: a + b, 2, b=3) == 5  # the slot was given back
    with pytest.raises(ValueError, match="limit"):
        WorkerPool(0)


# ========================================================================= registry


def test_registry_names_and_unknown_driver() -> None:
    assert DRIVER_NAMES == ("noise", "sim", "sigmf", "soapy")
    assert set(DRIVER_NAMES) <= set(driver_names())
    assert isinstance(get_driver("noise", lookback_s=3.0), NoiseDriver)
    assert isinstance(get_driver("soapy", device="driver=null"), SoapyDriver)
    with pytest.raises(ValueError, match=r"unknown driver 'rtl'.*noise, sim, sigmf, soapy"):
        get_driver("rtl")
    with pytest.raises(ValueError, match="refused its options"):
        get_driver("noise", no_such_option=1)
    with pytest.raises(ValueError, match="refused its options"):
        get_driver("sigmf")  # path is required


def test_registry_importing_a_driver_does_not_drag_in_the_others() -> None:
    import subprocess

    code = (
        "import sys; from petal_locate.node.drivers import get_driver; get_driver('noise');"
        "import petal_locate.node; mods = sys.modules;"
        "print(any(m.endswith(('drivers.sigmf', 'drivers.soapy', 'drivers.sim')) for m in mods),"
        " 'SoapySDR' in mods)"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["False", "False"]


def test_registry_sim_slot_is_lazy_and_says_so_when_the_simulator_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "petal_locate.node.drivers.sim"
    if importlib.util.find_spec(name) is None:
        with pytest.raises(CaptureError, match="driver 'sim' is not available") as caught:
            get_driver("sim")
        assert caught.value.code == "driver"

    # once a module of that name exists, the slot hands its SimDriver the options
    class SimDriver:
        def __init__(self, **options: Any) -> None:
            self.options = options

    stand_in = types.ModuleType(name)
    stand_in.SimDriver = SimDriver  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, name, stand_in)
    built = get_driver("sim", seed=4)
    assert isinstance(built, SimDriver) and built.options == {"seed": 4}
    monkeypatch.setitem(sys.modules, name, types.ModuleType(name))  # module without the class
    with pytest.raises(CaptureError, match="has no SimDriver"):
        get_driver("sim")


def test_register_driver_adds_a_name(monkeypatch: pytest.MonkeyPatch) -> None:
    from petal_locate.node import drivers as registry

    monkeypatch.setattr(registry, "_registered", {})
    register_driver("bench", lambda **options: ("bench", options))  # type: ignore[arg-type, return-value]
    assert get_driver("bench", gain=3) == ("bench", {"gain": 3})  # type: ignore[comparison-overlap]
    assert driver_names() == (*DRIVER_NAMES, "bench")
    with pytest.raises(ValueError, match="factory"):
        register_driver("x", None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="name"):
        register_driver("", lambda: None)  # type: ignore[arg-type, return-value]


# ============================================================================ noise


def test_noise_samples_are_deterministic_per_channel_and_start_time() -> None:
    a = noise_samples(0, "ch-a", T0, 4096)
    assert a.dtype == np.complex64 and a.shape == (4096,)
    assert np.array_equal(a, noise_samples(0, "ch-a", T0, 4096))  # same key, same samples
    assert np.array_equal(a[:1000], noise_samples(0, "ch-a", T0, 1000))  # prefix property
    for other in (
        noise_samples(0, "ch-b", T0, 4096),
        noise_samples(0, "ch-a", T0 + 1, 4096),
        noise_samples(1, "ch-a", T0, 4096),
    ):
        assert abs(np.vdot(a, other)) / 4096 < 0.1  # unrelated
    assert noise_samples(0, "ch-a", T0, 0).shape == (0,)


def test_noise_samples_are_unit_variance_white_circular_gaussian() -> None:
    n = 400_000
    x = noise_samples(3, "ch-a", T0, n).astype(np.complex128)
    sigma = 1 / np.sqrt(n)  # standard error of the mean power
    assert abs(np.mean(np.abs(x) ** 2) - 1.0) < 5 * sigma
    assert abs(np.mean(x.real**2) - 0.5) < 5 * sigma and abs(np.mean(x.imag**2) - 0.5) < 5 * sigma
    assert abs(np.mean(x)) < 5 * sigma
    assert abs(np.mean(x.real * x.imag)) < 5 * sigma  # I and Q uncorrelated
    assert abs(np.mean(x * x)) < 5 * sigma  # circular
    for lag in (1, 2, 17):
        assert abs(np.vdot(x[:-lag], x[lag:])) / n < 5 * sigma  # white
    assert np.mean(x.real**4) / np.mean(x.real**2) ** 2 == pytest.approx(3.0, abs=0.05)
    # and through the Welch estimator: 0 dB per bin in power
    power = 10 ** (welch_psd_db(x, 1e6, 256, 1024) / 10)
    assert np.mean(power) == pytest.approx(1.0, abs=0.01)
    assert np.max(np.abs(10 * np.log10(power))) < 0.8


async def test_noise_driver_waits_until_the_clock_has_passed_the_interval() -> None:
    clock = ManualClock(T0)
    driver = NoiseDriver(clock=clock)
    assert driver.capabilities.lookback_s == 30.0  # advertised
    await driver.open(channel("ch-n"), context())
    start, n, fs = T0 + 2_000_000, 1000, 1e6  # 2 ms ahead, 1 ms long
    task = asyncio.create_task(driver.read(start, n, fs, 437e6))
    await settle()
    assert not task.done()
    clock.set(start + 1_000_000 - 1)  # one nanosecond before the last sample has passed
    await settle()
    assert not task.done()
    clock.advance(1)
    capture = await finish(task)
    assert capture.t_first_ns == start and (capture.fs, capture.fc) == (fs, 437e6)
    assert not capture.overflow and capture.clock_locked
    assert np.array_equal(capture.iq, noise_samples(0, "ch-n", start, n))
    # any rate and centre, the same samples for the same channel and start
    clock.advance(NS)  # at 48 kHz the same 1000 samples last 20.8 ms
    again = await finish(asyncio.create_task(driver.read(start, n, 48_000.0, 1.2e9)))
    assert np.array_equal(again.iq, capture.iq) and (again.fs, again.fc) == (48_000.0, 1.2e9)
    assert await driver.psd(start, 437e6, 1e6, 64, 2) is None  # the monitor must read IQ
    clock.set_status(locked=False)
    unlocked = await finish(asyncio.create_task(driver.read(start, 10, fs, 437e6)))
    assert unlocked.clock_locked is False
    await driver.close()


async def test_noise_driver_lookback_range_checks_and_life_cycle() -> None:
    clock = ManualClock(T0)
    driver = NoiseDriver(
        clock=clock, lookback_s=2.0, max_sample_rate_hz=1e6, min_hz=100e6, max_hz=500e6, seed=9
    )
    with pytest.raises(CaptureError, match="not open") as caught:
        await driver.read(T0, 10, 1e6, 200e6)
    assert caught.value.code == "driver"
    await driver.open(channel(), context())

    past = await driver.read(T0 - 2 * NS, 100, 1e6, 200e6)  # at the lookback limit: served
    assert past.t_first_ns == T0 - 2 * NS
    assert np.array_equal(past.iq, noise_samples(9, "ch-a", T0 - 2 * NS, 100))
    with pytest.raises(CaptureError, match="lookback is 2 s") as caught:
        await driver.read(T0 - 2 * NS - 1, 100, 1e6, 200e6)
    assert caught.value.code == "too_late"
    for n, fs, fc in (
        (100, 1.5e6, 200e6),
        (100, 0.0, 200e6),
        (100, float("nan"), 200e6),
        (100, 1e6, 50e6),
        (100, 1e6, 600e6),
        (0, 1e6, 200e6),
        (2**24, 1e6, 200e6),
    ):
        with pytest.raises(CaptureError) as caught:
            await driver.read(T0 - NS, n, fs, fc)
        assert caught.value.code == "out_of_range", (n, fs, fc)

    waiting = asyncio.create_task(driver.read(T0 + NS, 10, 1e6, 200e6))
    await settle()
    await driver.close()
    await driver.close()  # idempotent
    clock.advance(2 * NS)
    with pytest.raises(CaptureError, match="closed during the read"):
        await finish(waiting)
    for bad in (dict(seed=1.5), dict(lookback_s=-1.0), dict(concurrent_reads=0)):
        with pytest.raises(ValueError):
            NoiseDriver(**bad)  # type: ignore[arg-type]


async def test_noise_driver_takes_clock_and_workers_from_the_context() -> None:
    clock, workers = ManualClock(T0), WorkerPool(1)
    driver = get_driver("noise")
    await driver.open(channel("ch-c"), context(clock, workers))
    task = asyncio.create_task(driver.read(T0, 64, 1e6, 1e9))
    await settle()
    assert not task.done()  # it waits on the node's clock, not on the wall clock
    clock.advance(64_000)
    assert (await finish(task)).t_first_ns == T0
    await driver.close()


# ============================================================================ sigmf

REC_FS, REC_FC, REC_N = 1e6, 435e6, 200_000
REC_DATETIME = "2026-03-01T12:00:00.250000001Z"
REC_T0 = int(dt.datetime(2026, 3, 1, 12, tzinfo=dt.UTC).timestamp()) * NS + 250_000_001
TONE_IN, TONE_OUT = 230e3, -400e3  # offsets from REC_FC: one inside the sub-band, one outside


def recording_samples() -> np.ndarray:
    k = np.arange(REC_N)
    return (
        np.exp(2j * np.pi * TONE_IN * k / REC_FS) + 0.5 * np.exp(2j * np.pi * TONE_OUT * k / REC_FS)
    ).astype(np.complex64)


def write_sigmf(
    directory: Path,
    samples: np.ndarray | None = None,
    name: str = "pass",
    global_changes: dict[str, Any] | None = None,
    capture_changes: dict[str, Any] | None = None,
) -> Path:
    """Write `<name>.sigmf-data` (cf32_le) and `<name>.sigmf-meta`; returns the base path."""
    data = recording_samples() if samples is None else samples
    data.astype("<c8").tofile(directory / f"{name}.sigmf-data")
    info: dict[str, Any] = {
        "core:datatype": "cf32_le", "core:sample_rate": REC_FS, "core:version": "1.0.0",
    }
    info.update(global_changes or {})
    first: dict[str, Any] = {
        "core:sample_start": 0, "core:frequency": REC_FC, "core:datetime": REC_DATETIME,
    }
    first.update(capture_changes or {})
    for section in (info, first):
        for key in [key for key, value in section.items() if value is None]:
            del section[key]
    meta = {"global": info, "captures": [first], "annotations": []}
    (directory / f"{name}.sigmf-meta").write_text(json.dumps(meta))
    return directory / name


async def open_sigmf(base: Path, clock: Any = None, **options: Any) -> SigMFDriver:
    options.setdefault("realtime", False)
    driver = SigMFDriver(path=base, **options)
    await driver.open(channel(), context(clock))
    return driver


def test_sigmf_datetime_is_read_to_the_nanosecond() -> None:
    whole = int(dt.datetime(2026, 3, 1, 12, tzinfo=dt.UTC).timestamp())
    assert ns_from_sigmf_datetime("2026-03-01T12:00:00Z") == whole * NS
    assert ns_from_sigmf_datetime(REC_DATETIME) == REC_T0
    assert ns_from_sigmf_datetime("2026-03-01T12:00:00.5") == whole * NS + NS // 2  # no zone: UTC
    assert ns_from_sigmf_datetime("2026-03-01T12:00:00.1234567891234Z") == whole * NS + 123_456_789
    assert ns_from_sigmf_datetime("2026-03-01T14:00:00+02:00") == whole * NS
    assert ns_from_sigmf_datetime("2026-03-01T06:30:00-0530") == whole * NS
    assert ns_from_sigmf_datetime("1970-01-01T00:00:00Z") == 0
    leap = int(dt.datetime(2028, 2, 29, 23, 59, 59, tzinfo=dt.UTC).timestamp())
    assert ns_from_sigmf_datetime("2028-02-29T23:59:59Z") == leap * NS
    for bad in ("2026-03-01", "2026-13-01T00:00:00Z", "2026-02-30T00:00:00Z",
                "2026-03-01T24:00:00Z", "yesterday", "1969-12-31T23:59:59Z", 5):
        with pytest.raises(ValueError, match="core:datetime"):
            ns_from_sigmf_datetime(bad)  # type: ignore[arg-type]


async def test_sigmf_honours_sample_rate_frequency_and_datetime(tmp_path: Path) -> None:
    base = write_sigmf(tmp_path)
    for path in (base, tmp_path / "pass.sigmf-meta", tmp_path / "pass.sigmf-data"):
        driver = await open_sigmf(path)
        recording = driver.recording
        assert recording is not None
        assert (recording.sample_rate_hz, recording.center_hz) == (REC_FS, REC_FC)
        assert recording.t0_ns == REC_T0 and recording.n_samples == REC_N
        caps = driver.capabilities
        assert (caps.min_hz, caps.max_hz) == (REC_FC - REC_FS / 2, REC_FC + REC_FS / 2)
        assert caps.max_sample_rate_hz == REC_FS
        assert caps.lookback_s == pytest.approx(REC_N / REC_FS)
        assert await driver.psd(REC_T0, REC_FC, REC_FS, 64, 1) is None
        await driver.close()
        with pytest.raises(CaptureError, match="not open"):
            _ = driver.capabilities
    assert isinstance(get_driver("sigmf", path=str(base), loop=True), SigMFDriver)


async def test_sigmf_native_read_is_bit_exact_and_reports_the_sample_time(
    tmp_path: Path,
) -> None:
    samples = recording_samples()
    driver = await open_sigmf(write_sigmf(tmp_path, samples))
    # a request between two samples gets the nearest one and is told its true time
    capture = await driver.read(REC_T0 + 12_345_000 + 400, 5000, REC_FS, REC_FC)
    assert capture.iq.tobytes() == samples[12_345:17_345].tobytes()
    assert capture.t_first_ns == REC_T0 + 12_345_000
    assert (capture.fs, capture.fc, capture.overflow, capture.clock_locked) == (
        REC_FS, REC_FC, False, True,
    )
    later = await driver.read(REC_T0 + 12_345_000 + 600, 10, REC_FS, REC_FC)
    assert later.t_first_ns == REC_T0 + 12_346_000
    assert later.iq.tobytes() == samples[12_346:12_356].tobytes()
    # the whole recording, to its last sample
    whole = await driver.read(REC_T0, REC_N, REC_FS, REC_FC)
    assert whole.iq.tobytes() == samples.tobytes()
    # outside the recording there is nothing to replay
    for start, n, where in (
        (REC_T0 - 1000, 10, "before its start"),
        (REC_T0 + (REC_N - 5) * 1000, 10, "past its end"),
        (REC_T0 + 3600 * NS, 10, "past its end"),
    ):
        with pytest.raises(CaptureError, match=where) as caught:
            await driver.read(start, n, REC_FS, REC_FC)
        assert caught.value.code == "too_late"
    with pytest.raises(CaptureError) as caught:
        await driver.read(REC_T0, 0, REC_FS, REC_FC)
    assert caught.value.code == "out_of_range"
    await driver.close()
    with pytest.raises(CaptureError, match="not open"):
        await driver.read(REC_T0, 10, REC_FS, REC_FC)


async def test_sigmf_loop_maps_any_time_onto_the_recording(tmp_path: Path) -> None:
    samples = recording_samples()
    driver = await open_sigmf(write_sigmf(tmp_path, samples), loop=True)
    period_ns = REC_N * 1000
    # three and a bit recordings later
    capture = await driver.read(REC_T0 + 3 * period_ns + 100_000, 64, REC_FS, REC_FC)
    assert capture.iq.tobytes() == samples[100:164].tobytes()
    assert capture.t_first_ns == REC_T0 + 3 * period_ns + 100_000
    # across the join: the last ten samples, then the first forty
    wrapped = await driver.read(REC_T0 + 7 * period_ns - 10_000, 50, REC_FS, REC_FC)
    assert wrapped.iq.tobytes() == np.concatenate([samples[-10:], samples[:40]]).tobytes()
    # before the recording was made, and a read longer than the recording
    early = await driver.read(REC_T0 - 5_000, 8, REC_FS, REC_FC)
    assert early.iq.tobytes() == np.concatenate([samples[-5:], samples[:3]]).tobytes()
    long = await driver.read(REC_T0, REC_N + 3, REC_FS, REC_FC)
    assert long.iq.tobytes() == np.concatenate([samples, samples[:3]]).tobytes()
    # years later, still exact (index arithmetic is integer)
    far = 10**9 * REC_N + 77
    year = await driver.read(REC_T0 + far * 1000, 4, REC_FS, REC_FC)
    assert year.iq.tobytes() == samples[77:81].tobytes() and year.t_first_ns == REC_T0 + far * 1000
    assert recording_slice(samples, -3, 5, False).tobytes() == np.concatenate(
        [np.zeros(3, np.complex64), samples[:2]]
    ).tobytes()


async def test_sigmf_retunes_and_resamples_inside_the_recorded_band(tmp_path: Path) -> None:
    driver = await open_sigmf(write_sigmf(tmp_path))
    first, n = 20_000, 6000
    fs, fc = 250e3, REC_FC + 200e3  # a quarter-rate window 200 kHz above the centre
    capture = await driver.read(REC_T0 + first * 1000, n, fs, fc)
    assert (capture.fs, capture.fc) == (fs, fc) and capture.iq.shape == (n,)
    assert capture.t_first_ns == REC_T0 + first * 1000
    # closed form: the tone at +230 kHz of the recording is at +30 kHz of the window;
    # its phase at the first sample is that of the recorded tone minus that of a mixer
    # running since sample 0. The tone at -400 kHz is outside and must be gone.
    phase0 = (TONE_IN - 200e3) * first / REC_FS
    expected = np.exp(2j * np.pi * (phase0 + 30e3 * np.arange(n) / fs))
    assert np.max(np.abs(capture.iq - expected)) < 3e-3
    spectrum = welch_psd_db(capture.iq, fs, 256, 16)
    assert int(np.argmax(spectrum)) == 128 + round(30e3 / (fs / 256))
    # another ratio (1/2) and shift: the kept tone lands at +130 kHz of the window
    half = await driver.read(REC_T0 + first * 1000, 4000, 500e3, REC_FC + 100e3)
    assert (half.fs, half.fc) == (500e3, REC_FC + 100e3)
    phase0 = (TONE_IN - 100e3) * first / REC_FS
    tone = np.exp(2j * np.pi * (phase0 + 130e3 * np.arange(4000) / 500e3))
    assert np.max(np.abs(half.iq - tone)) < 3e-3


async def test_sigmf_refuses_requests_outside_the_recorded_band(tmp_path: Path) -> None:
    driver = await open_sigmf(write_sigmf(tmp_path))
    for fs, fc in (
        (250e3, REC_FC + 450e3),  # upper edge beyond the recording
        (250e3, REC_FC - 450e3),
        (2e6, REC_FC),  # faster than recorded
        (REC_FS, REC_FC + 1e3),  # full rate off centre
        (250e3, 100e6),  # another band altogether
    ):
        with pytest.raises(CaptureError, match="outside the available band") as caught:
            await driver.read(REC_T0, 100, fs, fc)
        assert caught.value.code == "out_of_range"


async def test_sigmf_replays_in_real_time_on_the_node_clock(tmp_path: Path) -> None:
    clock = ManualClock(REC_T0 + 3600 * NS)  # an hour after the recording was made
    driver = await open_sigmf(write_sigmf(tmp_path), clock, loop=True, realtime=True)
    start = clock.now_ns() + 1_000_000
    task = asyncio.create_task(driver.read(start, 2000, REC_FS, REC_FC))
    await settle()
    assert not task.done()
    clock.set(start + 2_000_000 - 1)
    await settle(20)
    assert not task.done()
    clock.advance(1)
    capture = await finish(task)
    index = (start - REC_T0) // 1000 % REC_N
    assert capture.iq.tobytes() == recording_samples()[index : index + 2000].tobytes()
    # the past is served at once, and without `loop` a past recording never waits
    assert (await finish(asyncio.create_task(driver.read(REC_T0, 10, REC_FS, REC_FC)))).iq.shape == (10,)
    waiting = asyncio.create_task(driver.read(clock.now_ns() + NS, 10, REC_FS, REC_FC))
    await settle()
    await driver.close()
    clock.advance(2 * NS)
    with pytest.raises(CaptureError, match="closed during the read"):
        await finish(waiting)


async def test_sigmf_sample_start_t0_override_and_dataset_name(tmp_path: Path) -> None:
    samples = recording_samples()[:4096]
    # the first capture segment starts at sample 1000: its datetime belongs to that sample
    base = write_sigmf(tmp_path, samples, "offset", capture_changes={"core:sample_start": 1000})
    recording = load_recording(base)
    assert recording.t0_ns == REC_T0 - 1_000_000
    # an explicit t0 replaces the recorded time (and makes a missing datetime acceptable)
    base = write_sigmf(tmp_path, samples, "undated", capture_changes={"core:datetime": None})
    with pytest.raises(ValueError, match=r"undated\.sigmf-meta.*core:datetime.*t0_ns"):
        load_recording(base)
    assert load_recording(base, t0_ns=T0).t0_ns == T0
    driver = await open_sigmf(base, t0_ns=T0)
    assert (await driver.read(T0 + 5000, 4, REC_FS, REC_FC)).iq.tobytes() == samples[5:9].tobytes()
    # a non-conforming dataset names its data file
    base = write_sigmf(tmp_path, samples, "named", global_changes={"core:dataset": "raw.bin"})
    (tmp_path / "named.sigmf-data").rename(tmp_path / "raw.bin")
    assert load_recording(base).n_samples == 4096


async def test_sigmf_unusable_recordings_fail_at_open_naming_file_and_field(
    tmp_path: Path,
) -> None:
    samples = recording_samples()[:1024]
    cases: list[tuple[str, dict[str, Any], dict[str, Any], str]] = [
        ("int16", {"core:datatype": "ci16_le"}, {}, "core:datatype.*only 'cf32_le'"),
        ("norate", {"core:sample_rate": None}, {}, "core:sample_rate"),
        ("zerorate", {"core:sample_rate": 0}, {}, "core:sample_rate"),
        ("nofreq", {}, {"core:frequency": None}, "core:frequency"),
        ("baddate", {}, {"core:datetime": "noon"}, "core:datetime"),
        ("header", {}, {"core:header_bytes": 16}, "core:header_bytes"),
        ("dataset", {"core:dataset": "../elsewhere.bin"}, {}, "core:dataset"),
    ]
    for name, global_changes, capture_changes, pattern in cases:
        base = write_sigmf(tmp_path, samples, name, global_changes, capture_changes)
        driver = SigMFDriver(path=base)
        with pytest.raises(CaptureError, match=rf"{name}\.sigmf-meta.*{pattern}") as caught:
            await driver.open(channel(), context())
        assert caught.value.code == "driver"
        await driver.close()  # safe after a failed open
    with pytest.raises(CaptureError, match=r"missing\.sigmf-meta: cannot be read"):
        await SigMFDriver(path=tmp_path / "missing").open(channel(), context())
    base = write_sigmf(tmp_path, samples, "torn")
    torn = tmp_path / "torn.sigmf-data"
    torn.write_bytes(torn.read_bytes() + b"\x00\x01\x02")  # not a whole sample
    with pytest.raises(CaptureError, match=r"torn\.sigmf-data.*whole, positive number"):
        await SigMFDriver(path=base).open(channel(), context())
    (tmp_path / "torn.sigmf-data").write_bytes(b"")
    with pytest.raises(CaptureError, match=r"torn\.sigmf-data"):
        await SigMFDriver(path=base).open(channel(), context())
    (tmp_path / "broken.sigmf-meta").write_text("{not json")
    with pytest.raises(CaptureError, match=r"broken\.sigmf-meta: not valid JSON"):
        await SigMFDriver(path=tmp_path / "broken").open(channel(), context())
    with pytest.raises(ValueError, match="loop"):
        SigMFDriver(path=base, loop="yes")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="path"):
        SigMFDriver(path="")


# ============================================================================ soapy


async def test_soapy_driver_fails_clearly_when_the_library_is_missing() -> None:
    if importlib.util.find_spec("SoapySDR") is not None:
        pytest.skip("SoapySDR is installed here")
    driver = SoapyDriver(device="driver=rtlsdr")
    with pytest.raises(CaptureError, match="not open") as caught:
        _ = driver.capabilities
    assert caught.value.code == "driver"
    with pytest.raises(CaptureError, match="SoapySDR Python bindings are not installed") as caught:
        await driver.open(channel("ch-a", window()), context())
    assert caught.value.code == "driver"
    with pytest.raises(CaptureError, match="not open"):
        await driver.read(T0, 10, 1e6, 437e6)
    await driver.close()  # safe after a failed open
    for bad in (dict(time_sync="gps"), dict(rf_channel=-1), dict(buffer_s=0.0),
                dict(concurrent_reads=0)):
        with pytest.raises(ValueError):
            SoapyDriver(**bad)  # type: ignore[arg-type]


def window(center_hz: float = 437e6, span_hz: float = 400e3) -> MonitorAssignment:
    return MonitorAssignment(
        monitor_id=f"m{center_hz:.0f}", center_hz=center_hz, span_hz=span_hz, nfft=64, navg=1,
        frame_rate_hz=2.0,
    )


def ramp(start: int, count: int) -> np.ndarray:
    """Samples whose value is their own index: easy to recognise after a ring trip."""
    return (np.arange(start, start + count) * (1 + 0.5j)).astype(np.complex64)


def test_sample_ring_time_lookup_wraparound_and_limits() -> None:
    ring = SampleRing(1000, 1e6)  # 1 ms of stream; one sample is 1000 ns
    assert ring.locate(T0, 10) is None and ring.needs_time()  # empty
    with pytest.raises(ValueError, match="needs a time"):
        ring.append(ramp(0, 10), None)
    assert ring.append(ramp(0, 600), T0) is False
    assert ring.append(ramp(600, 600), None) is False  # no timestamp: continues by count
    assert (ring.written, ring.oldest, ring.gaps, ring.capacity, ring.fs) == (1200, 200, 0, 1000, 1e6)
    assert not ring.needs_time()

    span = ring.locate(T0 + 250_000, 100)
    assert span is not None and (span.index, span.t_first_ns, span.ready) == (250, T0 + 250_000, True)
    assert np.array_equal(ring.read(250, 100), ramp(250, 100))
    assert np.array_equal(ring.read(950, 100), ramp(950, 100))  # across the physical wrap
    assert np.array_equal(ring.read(200, 1000), ramp(200, 1000))  # everything it holds
    # the nearest sample, and its own time
    near = ring.locate(T0 + 250_400, 1)
    far = ring.locate(T0 + 250_600, 1)
    assert near is not None and far is not None
    assert (near.index, near.t_first_ns) == (250, T0 + 250_000)
    assert (far.index, far.t_first_ns) == (251, T0 + 251_000)
    assert ring.time_of(1199) == T0 + 1_199_000 and ring.time_of(5000) == T0 + 5_000_000

    # the past that was overwritten
    for kwargs in (dict(t_start_ns=T0 + 100_000, count=10),
                   dict(t_start_ns=T0 + 250_000, count=10, before=51),
                   dict(t_start_ns=T0 - 5_000_000, count=10)):
        with pytest.raises(CaptureError) as caught:
            ring.locate(**kwargs)  # type: ignore[arg-type]
        assert caught.value.code == "too_late"
    assert ring.locate(T0 + 250_000, 10, before=50) is not None  # exactly the oldest

    # the future: located, not ready until it has been written, context included
    ahead = ring.locate(T0 + 1_500_000, 100)
    assert ahead is not None and (ahead.index, ahead.ready) == (1500, False)
    edge = ring.locate(T0 + 1_150_000, 40, after=20)
    assert edge is not None and not edge.ready  # needs up to 1210, has 1200
    with pytest.raises(ValueError, match="beyond the samples written"):
        ring.read(1150, 60)
    ring.append(ramp(1200, 500), None)
    ahead = ring.locate(T0 + 1_500_000, 100)
    assert ahead is not None and ahead.ready
    assert np.array_equal(ring.read(1500, 100), ramp(1500, 100))
    with pytest.raises(CaptureError, match="overwritten"):
        ring.read(250, 10)  # located earlier, gone by now

    # a block larger than the ring keeps its tail, with indices and times intact
    big = SampleRing(100, 1e6)
    big.append(ramp(0, 350), T0)
    assert (big.written, big.oldest) == (350, 250)
    assert np.array_equal(big.read(250, 100), ramp(250, 100))
    assert big.time_of(300) == T0 + 300_000
    for bad in (dict(capacity=0, fs=1e6), dict(capacity=10, fs=0.0),
                dict(capacity=10, fs=1e6, tolerance_samples=0.0)):
        with pytest.raises(ValueError):
            SampleRing(**bad)  # type: ignore[arg-type]


def test_sample_ring_timestamp_jitter_gaps_and_overflow() -> None:
    ring = SampleRing(10_000, 1e6)
    ring.append(ramp(0, 1000), T0)
    # timestamps that wobble by less than half a sample do not split the stream, and
    # the time base stays anchored to the first block (no accumulation)
    assert ring.append(ramp(1000, 1000), T0 + 1_000_000 + 300) is False
    assert ring.append(ramp(2000, 1000), T0 + 2_000_000 - 400) is False
    assert ring.gaps == 0 and ring.time_of(2500) == T0 + 2_500_000

    # 5000 samples lost: the next block arrives 5 ms later than its count says
    assert ring.append(ramp(3000, 1000), T0 + 8_000_000) is True
    assert ring.gaps == 1 and ring.written == 4000
    before_gap = ring.locate(T0 + 2_900_000, 100)
    assert before_gap is not None and before_gap.index == 2900 and before_gap.ready
    after_gap = ring.locate(T0 + 8_250_000, 100)
    assert after_gap is not None
    assert (after_gap.index, after_gap.t_first_ns, after_gap.ready) == (3250, T0 + 8_250_000, True)
    assert np.array_equal(ring.read(after_gap.index, 100), ramp(3250, 100))
    assert ring.time_of(2999) == T0 + 2_999_000 and ring.time_of(3000) == T0 + 8_000_000
    for kwargs, what in (
        (dict(t_start_ns=T0 + 5_000_000, count=10), "at the requested time"),  # in the hole
        (dict(t_start_ns=T0 + 2_950_000, count=100), "inside the requested interval"),
        (dict(t_start_ns=T0 + 2_950_000, count=10, after=60), "inside the requested interval"),
        (dict(t_start_ns=T0 + 8_010_000, count=10, before=20), "just before"),
    ):
        with pytest.raises(CaptureError, match=what) as caught:
            ring.locate(**kwargs)  # type: ignore[arg-type]
        assert caught.value.code == "overflow"

    # an overflow reported without a usable timestamp: the next block must bring one
    ring.mark_gap()
    assert ring.needs_time()
    with pytest.raises(ValueError, match="needs a time"):
        ring.append(ramp(4000, 10), None)
    assert ring.append(ramp(4000, 10), T0 + 20_000_000) is True and ring.gaps == 2
    # segments that have been overwritten are forgotten
    ring.append(ramp(4010, 10_000), None)
    with pytest.raises(CaptureError) as caught:
        ring.locate(T0 + 8_250_000, 10)
    assert caught.value.code == "too_late"


def test_choose_stream_covers_the_monitor_windows() -> None:
    monitors = [window(251e6, 200e3), window(252e6, 200e3)]  # 250.9 .. 252.1 MHz
    rates = [8e6, 250e3, 2e6, 1e6]
    assert choose_stream(monitors, rates) == (251.5e6, 2e6)  # lowest rate >= 1.25 * 1.2 MHz
    assert choose_stream(monitors, [250e3, 1e6]) == (251.5e6, 1e6)  # none is enough: the highest
    assert choose_stream(monitors, []) == (251.5e6, pytest.approx(1.5e6))  # device lists none
    assert choose_stream(monitors, rates, center_hz=251e6) == (251e6, 8e6)  # needs 2 * 1.1 * 1.25
    assert choose_stream(monitors, rates, sample_rate_hz=4e6) == (251.5e6, 4e6)
    assert choose_stream([], rates, center_hz=100e6, sample_rate_hz=1e6) == (100e6, 1e6)
    with pytest.raises(ValueError, match="center_hz"):
        choose_stream([], rates)
    with pytest.raises(ValueError, match="sample_rate_hz"):
        choose_stream([], rates, center_hz=100e6)
    with pytest.raises(ValueError, match="sample_rate_hz"):
        choose_stream(monitors, rates, sample_rate_hz=-1.0)


def test_next_pps_time_is_the_second_after_the_edge_just_seen() -> None:
    second = 1_800_000_123
    assert next_pps_time_ns(second * NS + 20_000_000) == (second + 1) * NS  # just after an edge
    assert next_pps_time_ns(second * NS) == (second + 1) * NS
    # a node clock that is 20 ms slow still names the right second
    assert next_pps_time_ns((second + 1) * NS - 20_000_000) == (second + 2) * NS


# ---- control flow against a software stand-in for the SoapySDR module (not hardware)


class StandInDevice:
    """Mimics the parts of `SoapySDR.Device` the driver uses, streaming a known tone.

    The stream is exp(j 2 pi tone_hz i / fs) for device sample counter `i`, stamped
    with device time `time0_ns + i / fs`. It runs about twice as fast as real time so
    that tests are short.
    """

    tone_hz = 130e3
    mtu = 2048

    def __init__(self, args: Any) -> None:
        self.args = args
        self.scenario: dict[str, Any] = StandInSoapy.scenario
        self.fs = 0.0
        self.fc = 0.0
        self.time0_ns = 0  # device time of sample 0
        self.counter = 0
        self.blocks = 0
        self.calls: list[tuple[Any, ...]] = []
        self.pps_polls = 0
        StandInSoapy.devices.append(self)

    def _note(self, *call: Any) -> None:
        self.calls.append(call)

    def setClockSource(self, name: str) -> None:
        self._note("clock_source", name)

    def setTimeSource(self, name: str) -> None:
        self._note("time_source", name)

    def listSampleRates(self, direction: int, channel: int) -> list[float]:
        return [250e3, 1e6, 2e6]

    def setSampleRate(self, direction: int, channel: int, rate: float) -> None:
        self.fs = rate

    def getSampleRate(self, direction: int, channel: int) -> float:
        return self.fs

    def setFrequency(self, direction: int, channel: int, frequency: float) -> None:
        self.fc = frequency

    def getFrequency(self, direction: int, channel: int) -> float:
        return self.fc

    def setBandwidth(self, direction: int, channel: int, bandwidth: float) -> None:
        self._note("bandwidth", bandwidth)

    def setAntenna(self, direction: int, channel: int, name: str) -> None:
        self._note("antenna", name)

    def setGain(self, direction: int, channel: int, gain: float) -> None:
        self._note("gain", gain)

    def hasGainMode(self, direction: int, channel: int) -> bool:
        return True

    def setGainMode(self, direction: int, channel: int, automatic: bool) -> None:
        self._note("agc", automatic)

    def hasHardwareTime(self, what: str = "") -> bool:
        return bool(self.scenario.get("hardware_time", True))

    def getHardwareTime(self, what: str = "") -> int:
        if what == "PPS":
            self.pps_polls += 1
            edges = self.scenario.get("pps_edge_after", 3)
            return 0 if edges is None or self.pps_polls <= edges else 1
        return self.time0_ns + round(self.counter * NS / self.fs) if self.fs else self.time0_ns

    def setHardwareTime(self, time_ns: int, what: str = "") -> None:
        self._note("set_time", time_ns, what)
        self.time0_ns = time_ns if what == "" else time.time_ns()

    def readSensor(self, key: str) -> str:
        return str(self.scenario.get("sensor", "true"))

    def setupStream(self, direction: int, fmt: str, channels: list[int]) -> str:
        self._note("setup", fmt, tuple(channels))
        return "stream-handle"

    def getStreamMTU(self, handle: str) -> int:
        return self.mtu

    def activateStream(self, handle: str) -> int:
        self._note("activate")
        return int(self.scenario.get("activate_status", 0))

    def deactivateStream(self, handle: str) -> int:
        self._note("deactivate")
        return 0

    def closeStream(self, handle: str) -> None:
        self._note("close")

    def readStream(
        self, handle: str, buffs: list[np.ndarray], count: int, timeoutUs: int = 100_000
    ) -> Any:
        time.sleep(0.001)
        self.blocks += 1
        if self.blocks == self.scenario.get("fail_at_block"):
            raise RuntimeError("usb device vanished")
        if self.blocks == self.scenario.get("overflow_at_block"):
            self.counter += self.scenario["lost_samples"]  # these never reach the host
            return types.SimpleNamespace(ret=StandInSoapy.SOAPY_SDR_OVERFLOW, flags=0, timeNs=0)
        if self.blocks % 7 == 0:
            return types.SimpleNamespace(ret=StandInSoapy.SOAPY_SDR_TIMEOUT, flags=0, timeNs=0)
        i = np.arange(self.counter, self.counter + count)
        buffs[0][:count] = stand_in_tone(i, self.fs)
        stamped = self.scenario.get("hardware_time", True)
        result = types.SimpleNamespace(
            ret=count,
            flags=StandInSoapy.SOAPY_SDR_HAS_TIME if stamped else 0,
            timeNs=self.time0_ns + round(self.counter * NS / self.fs) if stamped else 0,
        )
        self.counter += count
        return result


def stand_in_tone(index: np.ndarray, fs: float) -> np.ndarray:
    return np.exp(2j * np.pi * StandInDevice.tone_hz * index / fs).astype(np.complex64)


class StandInSoapy(types.ModuleType):
    """A module object that looks like `SoapySDR` to the driver."""

    SOAPY_SDR_RX = 1
    SOAPY_SDR_CF32 = "CF32"
    SOAPY_SDR_HAS_TIME = 1 << 2
    SOAPY_SDR_TIMEOUT = -1
    SOAPY_SDR_OVERFLOW = -4
    Device = StandInDevice
    scenario: dict[str, Any] = {}
    devices: list[StandInDevice] = []

    @staticmethod
    def errToStr(code: int) -> str:
        return {-1: "TIMEOUT", -2: "STREAM_ERROR", -4: "OVERFLOW"}.get(code, "UNKNOWN")


@pytest.fixture
def stand_in(monkeypatch: pytest.MonkeyPatch) -> type[StandInSoapy]:
    StandInSoapy.scenario = {}
    StandInSoapy.devices = []
    monkeypatch.setitem(sys.modules, "SoapySDR", StandInSoapy("SoapySDR"))
    return StandInSoapy


async def test_soapy_control_flow_against_a_stand_in_device(stand_in: type[StandInSoapy]) -> None:
    driver = SoapyDriver(
        device={"driver": "standin"}, gain_db=30.0, antenna="RX2", bandwidth_hz=800e3,
        clock_source="external", buffer_s=1.0,
    )
    await driver.open(channel("ch-s", window(437e6, 400e3)), context())
    try:
        (device,) = stand_in.devices
        assert device.args == {"driver": "standin"}
        # the stream covers the monitor window: its centre, the lowest listed rate >= 500 kHz
        assert (device.fc, device.fs) == (437e6, 1e6)
        caps = driver.capabilities
        assert (caps.min_hz, caps.max_hz, caps.max_sample_rate_hz) == (436.5e6, 437.5e6, 1e6)
        assert caps.lookback_s == pytest.approx(0.4) and caps.concurrent_reads == 4
        for call in (("clock_source", "external"), ("gain", 30.0), ("antenna", "RX2"),
                     ("bandwidth", 800e3), ("setup", "CF32", (0,)), ("activate",)):
            assert call in device.calls
        # time_sync "host": the device clock was set from the node clock, at once
        (set_time,) = [call for call in device.calls if call[0] == "set_time"]
        assert set_time[2] == "" and abs(set_time[1] - time.time_ns()) < NS
        time0 = device.time0_ns

        # a read at the stream's own rate and centre: the very samples, their hardware time
        start = time0 + 20_000_000 + 300  # between two samples
        capture = await asyncio.wait_for(driver.read(start, 5000, 1e6, 437e6), 5.0)
        assert capture.t_first_ns == time0 + 20_000_000
        assert capture.iq.tobytes() == stand_in_tone(np.arange(20_000, 25_000), 1e6).tobytes()
        assert (capture.fs, capture.fc, capture.overflow, capture.clock_locked) == (
            1e6, 437e6, False, True,
        )
        # digital down-conversion from the stream: 250 kS/s, 100 kHz above the stream centre
        first = 60_000
        ddc = await asyncio.wait_for(
            driver.read(time0 + first * 1000, 3000, 250e3, 437.1e6), 5.0
        )
        assert (ddc.fs, ddc.fc, ddc.t_first_ns) == (250e3, 437.1e6, time0 + first * 1000)
        phase0 = (StandInDevice.tone_hz - 100e3) * first / 1e6
        expected = np.exp(2j * np.pi * (phase0 + 30e3 * np.arange(3000) / 250e3))
        assert np.max(np.abs(ddc.iq - expected)) < 3e-3
        # lookback: the recent past is still in the ring
        again = await asyncio.wait_for(driver.read(start, 5000, 1e6, 437e6), 5.0)
        assert again.iq.tobytes() == capture.iq.tobytes()
        assert await driver.psd(start, 437e6, 400e3, 64, 1) is None

        for args, code in (
            ((time0 - NS, 100, 1e6, 437e6), "too_late"),  # before the stream started
            ((start, 100, 250e3, 437.45e6), "out_of_range"),  # band beyond the stream's edge
            ((start, 100, 2e6, 437e6), "out_of_range"),  # faster than the stream
            ((start, 450_000, 1e6, 437e6), "out_of_range"),  # longer than 0.4 of the ring
            ((start, 0, 1e6, 437e6), "out_of_range"),
        ):
            with pytest.raises(CaptureError) as caught:
                await asyncio.wait_for(driver.read(*args), 5.0)
            assert caught.value.code == code, args
        with pytest.raises(CaptureError, match="already open"):
            await driver.open(channel("ch-s"), context())
    finally:
        await driver.close()
    assert ("deactivate",) in device.calls and ("close",) in device.calls
    assert not [t for t in threading.enumerate() if t.name == "soapy-ch-s"]
    await driver.close()  # idempotent
    with pytest.raises(CaptureError, match="not open"):
        await driver.read(T0, 10, 1e6, 437e6)


async def test_soapy_stand_in_overflow_splits_the_stream(stand_in: type[StandInSoapy]) -> None:
    stand_in.scenario = {"overflow_at_block": 30, "lost_samples": 5000}
    driver = SoapyDriver(buffer_s=1.0)
    await driver.open(channel("ch-s", window()), context())
    try:
        (device,) = stand_in.devices
        time0 = device.time0_ns
        # device samples 51200.. are lost for 5000 samples (blocks 7, 14, 21, 28 timed out)
        lost_from = 25 * StandInDevice.mtu
        good = await asyncio.wait_for(driver.read(time0 + 70_000_000, 2000, 1e6, 437e6), 5.0)
        assert good.t_first_ns == time0 + 70_000_000  # hardware time, not a sample count
        assert good.iq.tobytes() == stand_in_tone(np.arange(70_000, 72_000), 1e6).tobytes()
        assert driver.overflows == 1
        for start_index, n in ((lost_from + 1000, 100), (lost_from - 500, 1000)):
            with pytest.raises(CaptureError) as caught:
                await asyncio.wait_for(
                    driver.read(time0 + start_index * 1000, n, 1e6, 437e6), 5.0
                )
            assert caught.value.code == "overflow"
        before = await asyncio.wait_for(
            driver.read(time0 + (lost_from - 3000) * 1000, 2000, 1e6, 437e6), 5.0
        )
        assert before.iq.tobytes() == stand_in_tone(
            np.arange(lost_from - 3000, lost_from - 1000), 1e6
        ).tobytes()
    finally:
        await driver.close()


async def test_soapy_stand_in_pps_time_sync(
    stand_in: type[StandInSoapy], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(soapy_module, "_PPS_SETTLE_S", 0.01)
    monkeypatch.setattr(soapy_module, "_SENSOR_PERIOD_S", 0.02)
    driver = SoapyDriver(time_sync="pps", time_source="external", lock_sensor="ref_locked")
    stand_in.scenario = {"sensor": "false"}
    await driver.open(channel("ch-s", window()), context())
    try:
        (device,) = stand_in.devices
        assert ("time_source", "external") in device.calls
        (set_time,) = [call for call in device.calls if call[0] == "set_time"]
        # armed for the PPS edge after the one it had just seen: a whole second, soon
        assert set_time[2] == "PPS" and set_time[1] % NS == 0
        assert 0 < set_time[1] - time.time_ns() < 2 * NS
        assert device.pps_polls >= 4  # it waited for an edge before arming
        start = device.time0_ns + 5_000_000
        capture = await asyncio.wait_for(driver.read(start, 100, 1e6, 437e6), 5.0)
        assert capture.clock_locked is False  # the reference sensor says unlocked
        stand_in.scenario["sensor"] = "true"
        await asyncio.sleep(0.15)  # the sensor is polled periodically (here every 20 ms)
        capture = await asyncio.wait_for(driver.read(start, 100, 1e6, 437e6), 5.0)
        assert capture.clock_locked is True
    finally:
        await driver.close()


async def test_soapy_stand_in_failures_are_reported_and_cleaned_up(
    stand_in: type[StandInSoapy], monkeypatch: pytest.MonkeyPatch
) -> None:
    # no PPS edge ever: open gives up, releases the device, and says why
    monkeypatch.setattr(soapy_module, "_PPS_TIMEOUT_S", 0.05)
    stand_in.scenario = {"pps_edge_after": None}
    driver = SoapyDriver(time_sync="pps")
    with pytest.raises(CaptureError, match="no PPS edge") as caught:
        await driver.open(channel("ch-s", window()), context())
    assert caught.value.code == "driver"
    # PPS sync on a device without a clock
    stand_in.scenario = {"hardware_time": False}
    with pytest.raises(CaptureError, match="needs a device with hardware time"):
        await SoapyDriver(time_sync="pps").open(channel("ch-s", window()), context())
    # activateStream refuses: the stream that was set up is closed again
    stand_in.scenario = {"activate_status": -2}
    with pytest.raises(CaptureError, match="activateStream failed: STREAM_ERROR"):
        await SoapyDriver().open(channel("ch-s", window()), context())
    assert ("close",) in stand_in.devices[-1].calls
    # no monitors and no explicit tuning: nothing to tune to
    stand_in.scenario = {}
    with pytest.raises(CaptureError, match="center_hz: required"):
        await SoapyDriver().open(channel("ch-s"), context())

    # a device without hardware time is labelled from the node clock and never "locked"
    stand_in.scenario = {"hardware_time": False}
    driver = SoapyDriver(buffer_s=1.0)
    await driver.open(channel("ch-s", window()), context())
    try:
        assert not [c for c in stand_in.devices[-1].calls if c[0] == "set_time"]
        started = time.time_ns()
        capture = await asyncio.wait_for(driver.read(started + 5_000_000, 500, 1e6, 437e6), 5.0)
        assert capture.clock_locked is False
        assert abs(capture.t_first_ns - (started + 5_000_000)) <= 500
    finally:
        await driver.close()

    # the stream dies under a read: the read fails with `driver`, and so do later ones
    stand_in.scenario = {"fail_at_block": 20}
    driver = SoapyDriver(buffer_s=1.0)
    await driver.open(channel("ch-s", window()), context())
    try:
        far = stand_in.devices[-1].time0_ns + 500_000_000
        for _ in range(2):
            with pytest.raises(CaptureError, match="usb device vanished") as caught:
                await asyncio.wait_for(driver.read(far, 100, 1e6, 437e6), 5.0)
            assert caught.value.code == "driver"
    finally:
        await driver.close()
