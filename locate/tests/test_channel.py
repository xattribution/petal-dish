"""Path rendering, the decimating channel filter and thermal noise (MATH.md section 4).

References are computed another way each time: an FFT phase ramp for a delay, exact
sample shifts, closed-form chirps and tones, a direct DTFT of the filter taps, and a
brute-force convolution for the decimator.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from petal_locate.core.channel import (
    DECIMATE_STOPBAND_DB,
    decimate,
    decimate_margin,
    decimation_fir,
    render_path,
    thermal_noise,
)
from petal_locate.core.signals import (
    ChirpWaveform,
    CwWaveform,
    NoiseWaveform,
    PskWaveform,
    key128,
)

KEY = key128("emitter|1|jammer-1|0")
T0_NS = 1_790_000_000_000_000_000


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.abs(x) ** 2)))


def _err_db(got: np.ndarray, ref: np.ndarray) -> float:
    return 10.0 * math.log10(float(np.mean(np.abs(got - ref) ** 2) / np.mean(np.abs(ref) ** 2)))


def _zero(t: np.ndarray) -> np.ndarray:
    return np.zeros_like(t)


# -------------------------------------------------------------------- render_path


def test_render_path_evaluates_envelope_at_delayed_time_and_rotates():
    """y[k] = a s(t_k - D(t_k)) exp(2j pi phi(t_k)) against the chirp's closed form."""
    bw, period = 25e3, 0.01
    w = ChirpWaveform(bw, period)
    fs, n = 100e3, 5000
    d0, d1 = 0.2512345, 3.1e-6  # seconds, seconds per second
    f0, f1 = 1234.5, -2.5  # Hz, Hz per second
    y = render_path(
        w, T0_NS, n, fs,
        lambda t: d0 + d1 * t,
        lambda t: 0.125 + f0 * t + 0.5 * f1 * t * t,
        0.37,
    )
    assert y.dtype == np.complex128 and y.shape == (n,)
    t = np.arange(n) / fs
    u = np.mod(t - (d0 + d1 * t), period)  # T0_NS is a multiple of the sweep period
    envelope = np.exp(1j * np.pi * bw * u * (u - period) / period)
    expected = 0.37 * envelope * np.exp(2j * np.pi * (0.125 + f0 * t + 0.5 * f1 * t * t))
    np.testing.assert_allclose(y, expected, rtol=0, atol=1e-9)


def test_phase_fn_is_in_cycles_and_positive_slope_is_positive_frequency():
    fs, n = 64e3, 4096
    f_bb = 37 * fs / n  # exactly on FFT bin 37
    y = render_path(CwWaveform(), T0_NS, n, fs, _zero, lambda t: f_bb * t, 2.0)
    spec = np.abs(np.fft.fft(y)) / n
    assert int(np.argmax(spec)) == 37
    assert spec[37] == pytest.approx(2.0, rel=1e-12)
    # a quarter cycle is a rotation by +j
    q = render_path(CwWaveform(), T0_NS, 4, fs, None, lambda t: 0.25 + 0.0 * t, 1.0)
    np.testing.assert_allclose(q, 1j * np.ones(4), atol=1e-15)


def test_only_the_fractional_part_of_a_large_phase_matters():
    """Carrier phases reach 1e8 cycles; whole cycles must not cost precision."""
    w = NoiseWaveform(KEY, 25e3)
    fs, n = 31_250.0, 2000
    frac = lambda t: 0.3 + 411.7 * t  # noqa: E731
    small = render_path(w, T0_NS, n, fs, _zero, frac, 1.0)
    for whole in (1.0e6, 123_456_789.0, -3.0e8):
        big = render_path(w, T0_NS, n, fs, _zero, lambda t, c=whole: c + frac(t), 1.0)
        # c + frac(t) itself is rounded to 1.5e-8 cycle at 1e8; nothing more is lost
        assert np.max(np.abs(big - small)) < 2 * np.pi * 4e-8 * np.max(np.abs(small))


@pytest.mark.parametrize("kind", ["noise", "psk"])
def test_constant_delay_matches_fft_phase_ramp(kind):
    """A delay applied through render_path against delaying the undelayed samples
    with exp(-2j pi f tau) in the frequency domain."""
    w = NoiseWaveform(KEY, 25e3) if kind == "noise" else PskWaveform(KEY, 18_000, 4, 0.35)
    fs, n = 40e3, 8000
    span = n / fs
    tau = 37.3 / fs + 0.2  # 0.2 s (exactly 8000 samples) plus 37.3 samples
    t = np.arange(n) / fs
    y0 = render_path(w, T0_NS, n, fs, _zero, _zero, 1.0)
    y1 = render_path(w, T0_NS + 200_000_000, n, fs, lambda x: np.full_like(x, tau), _zero, 1.0)
    # Hann taper: makes the stretch periodic so the FFT delay is exact. The taper is
    # delayed with the signal, so compare against the taper evaluated at t - 37.3/fs.
    taper = lambda x: np.sin(np.pi * np.mod(x, span) / span) ** 2  # noqa: E731
    f = np.fft.fftfreq(n, 1.0 / fs)
    delayed = np.fft.ifft(np.fft.fft(taper(t) * y0) * np.exp(-2j * np.pi * f * 37.3 / fs))
    assert _err_db(delayed, taper(t - 37.3 / fs) * y1) < -50.0
    # sensitivity: a delay wrong by 2 % of a sample is far above that floor
    wrong = lambda x: np.full_like(x, tau + 0.02 / fs)  # noqa: E731
    y_bad = render_path(w, T0_NS + 200_000_000, n, fs, wrong, _zero, 1.0)
    assert _err_db(delayed, taper(t - 37.3 / fs) * y_bad) > -40.0


def test_integer_sample_delay_is_an_exact_shift():
    w = NoiseWaveform(KEY, 25e3)
    fs, n, m = 50e3, 3000, 173  # 20 000 ns per sample
    y0 = render_path(w, T0_NS, n, fs, _zero, _zero, 1.0)
    yd = render_path(w, T0_NS, n, fs, lambda t: np.full_like(t, m / fs), _zero, 1.0)
    assert np.max(np.abs(yd[m:] - y0[:-m])) < 1e-9
    # the same samples labelled from a later first-sample time
    later = render_path(w, T0_NS + m * 20_000, n - m, fs, _zero, _zero, 1.0)
    assert np.max(np.abs(later - y0[m:])) < 1e-9


def test_differential_delay_and_doppler_put_the_ambiguity_peak_at_tdoa_fdoa():
    """MATH.md section 6 signs: chi(tau, nu) = sum y_A(t + tau) conj(y_B(t)) exp(-2j pi nu t)
    peaks at tau = D_A - D_B and nu = f_bb,A - f_bb,B."""
    w = NoiseWaveform(KEY, 25e3)
    fs, n = 50e3, 8192
    d_a, d_b = 0.2400 + 40 / fs, 0.2400 + 29 / fs  # TDOA_AB = +11 samples
    n_fft = 4096
    f_a, f_b = 21 * fs / n_fft, 60 * fs / n_fft  # FDOA_AB = -39 bins of the product FFT
    ya = render_path(w, T0_NS, n, fs, lambda t: np.full_like(t, d_a), lambda t: f_a * t, 1.0)
    yb = render_path(w, T0_NS, n, fs, lambda t: np.full_like(t, d_b), lambda t: f_b * t, 1.0)
    lags = np.arange(-40, 41)
    t = np.arange(n_fft) / fs
    core = slice(100, 100 + n_fft)

    def chi(lag: int, nu: float) -> complex:
        shifted = ya[100 + lag : 100 + lag + n_fft]  # y_A(t_n + tau)
        return complex(np.sum(shifted * np.conj(yb[core]) * np.exp(-2j * np.pi * nu * t)))

    delay_cut = np.abs([chi(lag, f_a - f_b) for lag in lags])
    assert lags[int(np.argmax(delay_cut))] == 11
    assert delay_cut.max() / n_fft > 0.95  # fully coherent at the true point
    doppler_cut = np.abs(np.fft.fft(ya[111 : 111 + n_fft] * np.conj(yb[core])))
    assert int(np.argmax(doppler_cut)) == (-39) % n_fft
    # swapping the channels flips both signs
    swapped = np.abs(np.fft.fft(yb[100 - 11 : 100 - 11 + n_fft] * np.conj(ya[core])))
    assert int(np.argmax(swapped)) == 39
    assert swapped.max() / n_fft > 0.95


def test_delay_rate_compresses_the_envelope_time_axis():
    """A delay shrinking at rate r makes the received envelope run faster by 1 + r."""
    w = NoiseWaveform(KEY, 25e3)
    fs, n = 50e3, 4000
    rate = 2.0e-3  # exaggerated so the effect is many samples
    y = render_path(w, T0_NS, n, fs, lambda t: 0.25 - rate * t, _zero, 1.0)
    t = np.arange(n) / fs
    direct = w.evaluate(T0_NS - 250_000_000, t * (1.0 + rate))
    assert np.max(np.abs(y - direct)) < 1e-8


def test_amplitude_scales_power_and_may_vary_per_sample():
    w = NoiseWaveform(KEY, 25e3)
    fs, n = 31_250.0, 20_000
    unit = render_path(w, T0_NS, n, fs, _zero, _zero, 1.0)
    y = render_path(w, T0_NS, n, fs, _zero, _zero, 3.0)
    np.testing.assert_allclose(y, 3.0 * unit, rtol=1e-15)
    assert np.mean(np.abs(y) ** 2) == pytest.approx(9.0, rel=0.05)
    ramp = np.linspace(0.0, 1.0, n)
    y = render_path(w, T0_NS, n, fs, _zero, _zero, ramp)
    np.testing.assert_allclose(y, ramp * unit, rtol=1e-15)
    y = render_path(w, T0_NS, n, fs, _zero, _zero, 2j)
    np.testing.assert_allclose(y, 2j * unit, rtol=1e-15)
    assert render_path(w, T0_NS, 0, fs, _zero, _zero, 1.0).shape == (0,)


def test_render_path_rejects_bad_arguments():
    w = CwWaveform()
    with pytest.raises(ValueError, match="n must"):
        render_path(w, T0_NS, -1, 1e3, _zero, _zero, 1.0)
    with pytest.raises(ValueError, match="n must"):
        render_path(w, T0_NS, 10.5, 1e3, _zero, _zero, 1.0)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="fs"):
        render_path(w, T0_NS, 10, 0.0, _zero, _zero, 1.0)
    with pytest.raises(ValueError, match="fs"):
        render_path(w, T0_NS, 10, float("nan"), _zero, _zero, 1.0)
    with pytest.raises(ValueError, match="delay_fn"):
        render_path(w, T0_NS, 10, 1e3, lambda t: np.zeros(3), _zero, 1.0)
    with pytest.raises(ValueError, match="delay_fn"):
        render_path(w, T0_NS, 10, 1e3, lambda t: np.full_like(t, np.nan), _zero, 1.0)
    with pytest.raises(ValueError, match="phase_fn"):
        render_path(w, T0_NS, 10, 1e3, _zero, lambda t: np.full_like(t, np.inf), 1.0)
    with pytest.raises(ValueError, match="phase_fn"):
        render_path(w, T0_NS, 10, 1e3, _zero, lambda t: 1j * t, 1.0)  # cycles are real
    with pytest.raises(ValueError, match="delay_fn"):
        render_path(w, T0_NS, 10, 1e3, lambda t: None, _zero, 1.0)
    with pytest.raises(ValueError, match="amplitude"):
        render_path(w, T0_NS, 10, 1e3, _zero, _zero, np.ones(4))
    with pytest.raises(ValueError, match="amplitude"):
        render_path(w, T0_NS, 10, 1e3, _zero, _zero, float("nan"))
    with pytest.raises(ValueError, match="base_ns"):
        render_path(w, 1.79e18, 10, 1e3, _zero, _zero, 1.0)  # type: ignore[arg-type]
    # scalars from the callbacks are broadcast
    y = render_path(w, T0_NS, 10, 1e3, lambda t: 0.1, lambda t: 0.5, 1.0)
    np.testing.assert_allclose(y, -np.ones(10), atol=1e-15)


# ----------------------------------------------------------------------- decimate


@pytest.mark.parametrize("m", [2, 3, 4, 8])
def test_decimation_filter_meets_the_channel_filter_spec(m):
    """Direct DTFT of the taps: passband to 0.4 fs, 60 dB (designed 80) beyond 0.5 fs."""
    h = decimation_fir(m)
    assert h.size == 52 * m + 1 and h.size % 2 == 1
    np.testing.assert_array_equal(h, h[::-1])  # linear phase
    assert np.sum(h) == pytest.approx(1.0, abs=1e-12)
    half = (h.size - 1) // 2
    j = np.arange(h.size) - half  # centred: the response is then real (zero phase)

    def response(f_out: np.ndarray) -> np.ndarray:  # f_out in units of the output rate
        return np.exp(-2j * np.pi * np.outer(f_out / m, j)) @ h

    passband = response(np.linspace(0.0, 0.4, 401))
    assert np.max(np.abs(passband - 1.0)) < 1e-3  # amplitude and phase to 0.1 %
    assert np.max(np.abs(passband.imag)) < 1e-12
    stopband = response(np.linspace(0.5, 0.5 * m, 400 * m))  # 25 points per sidelobe
    assert 20 * np.log10(np.max(np.abs(stopband))) < -DECIMATE_STOPBAND_DB
    assert DECIMATE_STOPBAND_DB >= 60.0
    assert not h.flags.writeable


@pytest.mark.parametrize("m", [2, 4, 5])
def test_decimate_keeps_in_band_tones_with_zero_delay(m):
    n_out = 2000
    k = np.arange(n_out * m)
    edge = decimate_margin(m)
    assert edge == 26
    for f_out in (0.0, 0.07, -0.25, 0.39):  # cycles per output sample
        x = 0.8 * np.exp(2j * np.pi * (f_out / m * k + 0.123))
        y = decimate(x, m)
        assert y.shape == (n_out,) and y.dtype == np.complex128
        ref = x[::m]  # the same instants: output sample i is input sample i * m
        core = slice(edge, n_out - edge)
        # amplitude and phase to 0.1 %, and therefore no residual delay
        assert np.max(np.abs(y[core] - ref[core])) < 1e-3 * 0.8
        assert np.max(np.abs(np.abs(y[core]) / 0.8 - 1.0)) < 1e-3
        assert np.max(np.abs(np.angle(y[core] * np.conj(ref[core])))) < 1e-3
    # sensitivity: one output sample of residual delay at 0.39 cycle/sample is a
    # 2.4 rad phase error, and one input sample (1 / m of that) is still 100x the limit
    x = 0.8 * np.exp(2j * np.pi * 0.39 / m * k)
    y = decimate(x, m)
    core = slice(edge + 1, n_out - edge)
    assert np.max(np.abs(np.roll(y, 1)[core] - x[::m][core])) > 0.5
    assert np.max(np.abs(y[core] - x[1::m][core])) > 0.1 * 0.8


@pytest.mark.parametrize("m", [2, 4, 5])
def test_decimate_rejects_out_of_band_tones_by_60_db(m):
    n_out = 2000
    k = np.arange(n_out * m)
    edge = decimate_margin(m)
    for f_out in (0.5, 0.55, 0.61, 0.9, -1.0, 0.5 * m - 0.01, 0.5 * m):
        x = np.exp(2j * np.pi * f_out / m * k)
        y = decimate(x, m)
        level = 20 * np.log10(np.max(np.abs(y[edge : n_out - edge])))
        assert level < -60.0, (m, f_out, level)


def test_decimate_equals_brute_force_filter_then_downsample():
    rng = np.random.default_rng(8)
    for m, n in ((2, 1001), (3, 3000), (7, 5000)):
        x = rng.standard_normal(n) + 1j * rng.standard_normal(n)
        h = decimation_fir(m)
        delay = (h.size - 1) // 2
        full = np.convolve(x, h)  # full[i] = sum_j h[j] x[i - j]
        expected = full[delay : delay + n][::m]  # group delay removed, every m-th sample
        got = decimate(x, m)
        assert got.shape == (math.ceil(n / m),)
        np.testing.assert_allclose(got, expected, rtol=0, atol=1e-12)


def test_decimate_impulse_response_is_centred_on_the_impulse():
    m, i0 = 4, 150
    x = np.zeros(1200)
    x[i0 * m] = 1.0
    y = decimate(x, m)
    assert y.dtype == np.float64 and y.shape == (300,)
    assert int(np.argmax(np.abs(y))) == i0
    np.testing.assert_allclose(y[i0 - 26 : i0 + 27], y[i0 - 26 : i0 + 27][::-1], rtol=0, atol=1e-15)
    assert np.all(y[: i0 - 26] == 0.0) and np.all(y[i0 + 27 :] == 0.0)


def test_decimate_of_an_oversampled_render_equals_the_direct_render():
    """The simulator's straddling path: synthesise at m fs and decimate. For a signal
    inside the passband the result must be the waveform sampled at fs."""
    w = NoiseWaveform(KEY, 15e3)  # +/- 7.5 kHz, inside 0.4 x 25 kHz
    fs, n = 25e3, 4000
    direct = render_path(w, T0_NS, n, fs, _zero, lambda t: 2000.0 * t, 1.0)
    for m in (2, 4):
        over = render_path(w, T0_NS, n * m, fs * m, _zero, lambda t: 2000.0 * t, 1.0)
        got = decimate(over, m)
        core = slice(decimate_margin(m), n - decimate_margin(m))
        assert _err_db(got[core], direct[core]) < -70.0


def test_decimate_removes_what_would_alias():
    """A chirp wider than the capture: after decimation only the in-band part is left."""
    m, fs = 4, 25e3
    w = ChirpWaveform(80e3, 0.01)  # sweeps +/- 40 kHz; the capture holds +/- 12.5 kHz
    n = 5000  # 0.2 s = 20 sweeps
    y = decimate(render_path(w, T0_NS, n * m, fs * m, _zero, _zero, 1.0), m)
    core = y[decimate_margin(m) : n - decimate_margin(m)]
    # A flat 80 kHz spectrum through the filter keeps (noise bandwidth) / 80 kHz, with
    # the noise bandwidth = input rate x sum(h^2) by Parseval (22.4 kHz: the response
    # is 6 dB down at 0.45 fs). Naive subsampling would keep (alias) all of it.
    kept = m * fs * float(np.sum(decimation_fir(m) ** 2)) / 80e3
    assert 0.27 < kept < 0.29
    assert np.mean(np.abs(core) ** 2) == pytest.approx(kept, rel=0.03)
    naive = render_path(w, T0_NS, n, fs, _zero, _zero, 1.0)
    assert np.mean(np.abs(naive) ** 2) == pytest.approx(1.0, rel=1e-9)


def test_decimate_shapes_and_validation():
    x = np.arange(10, dtype=float)
    np.testing.assert_array_equal(decimate(x, 1), x)
    assert decimate(x, 1) is not x
    assert decimate(x, 3).shape == (4,)  # ceil(10 / 3): samples at inputs 0, 3, 6, 9
    assert decimate(np.empty(0), 4).shape == (0,)
    assert decimate(np.arange(8), 2).dtype == np.float64  # integers are promoted
    assert decimate_margin(1) == 0
    with pytest.raises(ValueError, match="m must"):
        decimate(x, 0)
    with pytest.raises(ValueError, match="m must"):
        decimate(x, 2.0)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="x must"):
        decimate(np.zeros((4, 4)), 2)
    with pytest.raises(ValueError, match="x must"):
        decimate(np.array(["a", "b"]), 2)
    with pytest.raises(ValueError, match="m must"):
        decimation_fir(1)
    decimation_fir(2)
    with pytest.raises(ValueError, match="m must"):
        decimation_fir(2.0)  # type: ignore[arg-type]


# ------------------------------------------------------------------ thermal noise


def test_thermal_noise_is_unit_power_circular_white_gaussian():
    n = 200_000
    x = thermal_noise(n, "thermal|1|hnl-a|1790000000000000000|31250.0|259000000.0")
    assert x.dtype == np.complex128 and x.shape == (n,)
    assert np.mean(np.abs(x) ** 2) == pytest.approx(1.0, abs=0.012)  # sigma 0.22 %
    assert np.var(x.real) == pytest.approx(0.5, abs=0.01)
    assert np.var(x.imag) == pytest.approx(0.5, abs=0.01)
    assert abs(np.mean(x)) < 0.012
    assert abs(np.mean(x * x)) < 0.012  # circular
    assert np.mean(np.abs(x) ** 4) == pytest.approx(2.0, abs=0.05)  # complex Gaussian
    spec = np.fft.fft(x, 2 * n)
    r = np.fft.ifft(spec.real**2 + spec.imag**2)[:n] / np.sum(np.abs(x) ** 2)
    assert np.max(np.abs(r[1:])) < 6.0 / math.sqrt(n)  # white at every lag


def test_thermal_noise_is_keyed_by_its_label():
    label = "thermal|1|hnl-a|1790000000000000000|31250.0|259000000.0"
    a = thermal_noise(50_000, label)
    np.testing.assert_array_equal(a, thermal_noise(50_000, label))
    np.testing.assert_array_equal(a[:1000], thermal_noise(1000, label))
    b = thermal_noise(50_000, label.replace("hnl-a", "hnl-b"))
    c = thermal_noise(50_000, label.replace("1790000000000000000", "1790000000000000001"))
    for other in (b, c):
        assert abs(np.vdot(a, other)) / 50_000 < 0.025  # sigma 0.0045
    # the documented construction
    rng = np.random.Generator(np.random.Philox(key=key128(label)))
    z = rng.standard_normal(2000)
    np.testing.assert_array_equal(a[:1000], (z[0::2] + 1j * z[1::2]) / np.sqrt(2.0))
    assert thermal_noise(0, label).shape == (0,)
    with pytest.raises(ValueError, match="n must"):
        thermal_noise(-1, label)
    with pytest.raises(ValueError, match="label"):
        thermal_noise(4, 17)  # type: ignore[arg-type]
