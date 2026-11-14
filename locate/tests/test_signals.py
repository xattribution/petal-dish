"""Deterministic waveforms (MATH.md section 4 and section 12 item 7).

Every check compares against something computed another way: the generator written
out as in MATH.md, a time-domain convolution, FFT (trigonometric) interpolation,
closed-form spectra, exact integer arithmetic, or the Welch estimator of spectrum.py.
"""
from __future__ import annotations

import enum
import hashlib
import math
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from petal_locate.core import signals
from petal_locate.core.signals import (
    BLOCK,
    ChirpWaveform,
    CwWaveform,
    NoiseWaveform,
    PskWaveform,
    Waveform,
    key128,
    raw_block,
    raw_normals,
    rrc_pulse,
    waveform_from_spec,
)
from petal_locate.core.spectrum import welch_psd_db

KEY = key128("emitter|1|jammer-1|0")
T0_NS = 1_790_000_000_000_000_000  # 2026-09-21, the epoch region of MATH.md 12.7


# ------------------------------------------------------------------------ helpers


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.abs(x) ** 2)))


def _err_db(got: np.ndarray, ref: np.ndarray) -> float:
    return 10.0 * math.log10(float(np.mean(np.abs(got - ref) ** 2) / np.mean(np.abs(ref) ** 2)))


def _psd(y: np.ndarray, fs: float, nfft: int) -> tuple[np.ndarray, np.ndarray]:
    """Welch spectrum (linear; unit white noise reads 1 per bin) and its frequency axis."""
    navg = len(y) // nfft
    p = 10.0 ** (welch_psd_db(y, fs, nfft, navg).astype(np.float64) / 10.0)
    f = (np.arange(nfft) - nfft // 2) * fs / nfft
    return f, p


def _occupied_bw(f: np.ndarray, p: np.ndarray, frac: float = 0.99) -> float:
    c = np.cumsum(p) / np.sum(p)
    lo = np.interp((1.0 - frac) / 2.0, c, f)
    hi = np.interp(1.0 - (1.0 - frac) / 2.0, c, f)
    return float(hi - lo)


def _autocorr(x: np.ndarray) -> np.ndarray:
    """Normalised autocorrelation at lags 0..N-1 (biased estimate, r[0] = 1)."""
    n = len(x)
    spec = np.fft.fft(x, 2 * n)
    r = np.fft.ifft(spec.real**2 + spec.imag**2)[:n]
    return r / r[0].real


def _fft_resample(x: np.ndarray, n_out: int) -> np.ndarray:
    """Trigonometric (band-limited, periodic) interpolation of x onto n_out points."""
    n = len(x)
    keep = min(n, n_out) // 2
    spec = np.fft.fft(x)
    out = np.zeros(n_out, dtype=np.complex128)
    out[:keep] = spec[:keep]
    out[-keep:] = spec[-keep:]
    return np.fft.ifft(out) * (n_out / n)


def _documented_normals(key: int, block: int) -> np.ndarray:
    """MATH.md section 4, written out independently of signals.raw_normals."""
    counter = np.array([0, block, 0, 0], dtype=np.uint64)
    return np.random.Generator(np.random.Philox(key=key, counter=counter)).standard_normal(8192)


# --------------------------------------------------------------------------- keys


def test_key128_is_the_documented_blake2b_hash():
    label = "emitter|7|jam|3"
    digest = hashlib.blake2b(label.encode("utf-8"), digest_size=16).digest()
    # little-endian: byte i has weight 256**i
    expected = sum(byte << (8 * i) for i, byte in enumerate(digest))
    assert key128(label) == expected
    assert 0 <= key128(label) < 2**128
    assert key128("relay|1|sat-a|x1") != key128("relay|1|sat-a|x2")
    with pytest.raises(ValueError, match="label"):
        key128(b"bytes")  # type: ignore[arg-type]


def test_keys_and_samples_do_not_depend_on_the_process():
    """Another interpreter with a different PYTHONHASHSEED gives identical values."""
    code = (
        "import numpy as np\n"
        "from petal_locate.core.signals import key128, waveform_from_spec\n"
        "w = waveform_from_spec({'type': 'noise', 'bandwidth_hz': 25e3}, 'emitter|1|j|0')\n"
        "y = w.evaluate(1_790_000_000_123_456_789, np.arange(5) / 31250.0)\n"
        "print(key128('emitter|1|j|0'), y.tobytes().hex())\n"
    )
    env = dict(os.environ, PYTHONHASHSEED="12345")
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True
    ).stdout.split()
    w = waveform_from_spec({"type": "noise", "bandwidth_hz": 25e3}, "emitter|1|j|0")
    y = w.evaluate(1_790_000_000_123_456_789, np.arange(5) / 31250.0)
    assert int(out[0]) == key128("emitter|1|j|0")
    assert out[1] == y.tobytes().hex()


# ------------------------------------------------------------ raw block generator


def test_raw_block_is_the_documented_philox_layout():
    for block in (0, 1, 437_011_719, 2**40 + 5):
        x = _documented_normals(KEY, block)
        m = np.arange(BLOCK)
        expected = (x[2 * m] + 1j * x[2 * m + 1]) / np.sqrt(2.0)
        np.testing.assert_array_equal(raw_normals(KEY, block), x)
        np.testing.assert_array_equal(raw_block(KEY, block), expected)
    # times before 1970: the counter word wraps modulo 2**64
    np.testing.assert_array_equal(raw_normals(KEY, -1), _documented_normals(KEY, 2**64 - 1))
    with pytest.raises(ValueError, match="key"):
        raw_block(2**128, 0)


def test_math12_item7_raw_blocks_are_white_at_every_lag():
    """MATH.md 12.7 (first half): 64 consecutive raw blocks, every non-zero lag < 0.02."""
    b0 = 437_011_700
    x = np.concatenate([raw_block(KEY, b0 + i) for i in range(64)])
    r = _autocorr(x)
    assert abs(np.mean(np.abs(x) ** 2) - 1.0) < 0.01
    assert np.max(np.abs(r[1:])) < 0.02
    assert abs(r[4094]) < 0.02
    assert abs(r[8188]) < 0.02


def test_block_index_in_the_low_counter_word_would_fail_item7():
    """Negative control for the test above: the layout MATH.md warns against really
    does make block b + 1 a copy of block b shifted by two complex samples."""
    blocks = []
    for b in range(64):
        counter = np.array([b, 0, 0, 0], dtype=np.uint64)
        x = np.random.Generator(np.random.Philox(key=KEY, counter=counter)).standard_normal(8192)
        blocks.append((x[0::2] + 1j * x[1::2]) / np.sqrt(2.0))
    r = _autocorr(np.concatenate(blocks))
    assert abs(r[4094]) > 0.5
    assert abs(r[8188]) > 0.5


# ----------------------------------------------------- pure function of absolute time


@pytest.mark.parametrize("bandwidth_hz", [25e3, 300e3])
def test_math12_item7_shifted_base_gives_the_same_noise(bandwidth_hz):
    """MATH.md 12.7 (second half): evaluate(b, t) == evaluate(b + 123456789, t - 0.123456789)."""
    rng = np.random.default_rng(12)
    w = NoiseWaveform(KEY, bandwidth_hz)
    t = np.arange(3000) / (1.25 * bandwidth_hz)
    for _ in range(4):
        b = 1_790_000_000_000_000_000 + int(rng.integers(0, 10**15))
        a = w.evaluate(b, t)
        c = w.evaluate(b + 123_456_789, t - 0.123456789)
        assert np.max(np.abs(a - c)) <= 1e-9 * _rms(a)
        np.testing.assert_allclose(c, a, rtol=1e-9, atol=1e-9 * _rms(a))


def _waveforms() -> dict[str, Waveform]:
    return {
        "cw": CwWaveform(),
        "chirp": ChirpWaveform(25e3, 0.01),
        "noise": NoiseWaveform(KEY, 25e3),
        "bpsk": PskWaveform(KEY, 16000, 2, 0.5),
        "qpsk": PskWaveform(KEY, 18519, 4, 0.35),
    }


@pytest.mark.parametrize("name", ["cw", "chirp", "noise", "bpsk", "qpsk"])
def test_same_instants_same_values_for_any_split(name):
    """Non-uniform and negative t_rel, shifts of nanoseconds to seconds, shuffled order."""
    w = _waveforms()[name]
    rng = np.random.default_rng(3)
    t = np.sort(rng.uniform(-0.05, 0.05, 2500))  # non-uniform, both signs
    ref = w.evaluate(T0_NS + 987_654_321, t)
    assert ref.dtype == np.complex128 and ref.shape == t.shape
    scale = _rms(ref)
    for shift_ns in (1, -1, 999, 123_456_789, -3_000_000_007, 5_000_000_000):
        got = w.evaluate(T0_NS + 987_654_321 + shift_ns, t - shift_ns * 1e-9)
        assert np.max(np.abs(got - ref)) <= 1e-9 * scale, shift_ns
    perm = rng.permutation(t.size)
    shuffled = w.evaluate(T0_NS + 987_654_321, t[perm])
    np.testing.assert_allclose(shuffled, ref[perm], rtol=0, atol=1e-12)
    # shape is preserved, scalars included
    assert w.evaluate(T0_NS, t[:6].reshape(2, 3)).shape == (2, 3)
    assert w.evaluate(T0_NS, 0.25).shape == ()


def test_continuous_through_the_unix_epoch():
    """Negative absolute times (negative grid and block indices) use the same grid."""
    w = NoiseWaveform(KEY, 25e3)
    t = np.arange(-400, 400) / 31250.0
    a = w.evaluate(0, t)
    b = w.evaluate(-7_000_000, t + 0.007)
    c = w.evaluate(11_000_000, t - 0.011)
    assert np.max(np.abs(a - b)) <= 1e-9 * _rms(a)
    assert np.max(np.abs(a - c)) <= 1e-9 * _rms(a)
    assert abs(_rms(a) - 1.0) < 0.2


def test_scattered_instants_match_dense_evaluation():
    """Instants spread over 1000 s (thousands of blocks apart) take the sparse path;
    each must equal the value from its own single-instant call on a fresh object."""
    rng = np.random.default_rng(5)
    t = rng.uniform(0.0, 1000.0, 300)
    w = NoiseWaveform(KEY, 25e3, cache_blocks=8)
    got = w.evaluate(T0_NS, t)
    fresh = NoiseWaveform(KEY, 25e3, cache_blocks=0)
    ref = np.array([fresh.evaluate(T0_NS, np.array([ti]))[0] for ti in t[:40]])
    np.testing.assert_allclose(got[:40], ref, rtol=0, atol=1e-12)
    assert abs(_rms(got) - 1.0) < 0.15
    assert len(w._cache) <= 8
    # hours apart: more blocks between the instants than the index mask may span
    far = np.array([0.0, 9000.0, 1.0, 9000.5, -7200.0])
    np.testing.assert_allclose(
        w.evaluate(T0_NS, far),
        [fresh.evaluate(T0_NS, np.array([ti]))[0] for ti in far],
        rtol=0,
        atol=1e-12,
    )


# ------------------------------------------------------------ grid, FIR and blocks


def test_grid_rate_is_integer_ceil_of_four_bandwidths():
    assert NoiseWaveform(KEY, 25e3).grid_rate_hz == 100_000
    w = NoiseWaveform(KEY, 12_345.6)
    assert isinstance(w.grid_rate_hz, int) and w.grid_rate_hz == math.ceil(4 * 12_345.6) == 49_383
    assert PskWaveform(KEY, 18519).grid_rate_hz == 4 * 18519


def test_evaluate_on_grid_instants_returns_grid_samples_exactly():
    """Grid sample k is the envelope at k / grid_rate_hz; integer arithmetic only."""
    w = NoiseWaveform(KEY, 25e3)  # 100 kHz grid: 10 000 ns per sample
    k0 = ((T0_NS // 10_000) // BLOCK + 1) * BLOCK - 6  # run across a block edge
    g = w.grid_samples(k0, 12)
    for i in range(12):
        v = w.evaluate((k0 + i) * 10_000, np.array([0.0]))[0]
        assert v == g[i]
    # the same instants reached through t_rel
    v = w.evaluate(k0 * 10_000, np.arange(12) * 1e-5)
    np.testing.assert_allclose(v, g, rtol=0, atol=1e-9 * _rms(g))


def test_grid_is_the_plain_convolution_across_block_edges():
    """g[k] = sum_j h[j] raw[k - j], checked by time-domain convolution over 3 blocks."""
    w = NoiseWaveform(KEY, 25e3)
    b = 437_011_719
    raw = np.concatenate([raw_block(KEY, b + i) for i in range(3)])
    full = np.convolve(raw, w.fir)  # full[i] = sum_j h[j] raw[i - j]
    assert w.fir.size == 4097 and abs(float(np.sum(w.fir**2)) - 1.0) < 1e-12
    # all of blocks b+1 and b+2 (their history lies inside the three raw blocks)
    got = w.grid_samples((b + 1) * BLOCK, 2 * BLOCK)
    np.testing.assert_allclose(got, full[BLOCK : 3 * BLOCK], rtol=0, atol=1e-12)


def test_block_values_do_not_depend_on_request_pattern_or_cache():
    one = NoiseWaveform(KEY, 25e3, cache_blocks=0)
    many = NoiseWaveform(KEY, 25e3, cache_blocks=3)
    k0 = 437_011_719 * BLOCK - 100
    ref = many.grid_samples(k0, 6 * BLOCK)
    for off in (0, 4000, 9000, 20000):
        np.testing.assert_array_equal(one.grid_samples(k0 + off, 300), ref[off : off + 300])
    assert len(many._cache) <= 3


def test_noise_filter_response_is_flat_then_90_db_down_at_the_band_edge():
    """Frequency response of the FIR by direct DTFT (not the FFT used at run time)."""
    w = NoiseWaveform(KEY, 25e3)
    h = w.fir
    j = np.arange(h.size)
    f_hz = np.concatenate([np.linspace(0, 12_300, 60), np.linspace(12_500, 50_000, 200)])
    resp = np.abs(np.exp(-2j * np.pi * np.outer(f_hz / w.grid_rate_hz, j)) @ h) ** 2
    passband = resp[f_hz <= 0.98 * 12_500]
    assert 10 * np.log10(passband.max() / passband.min()) < 0.01
    assert 10 * np.log10(resp[f_hz >= 12_500].max() / passband.mean()) < -85.0


def test_interpolation_kernel_error_is_below_minus_100_db():
    """16-tap Kaiser sinc against exact complex exponentials up to 0.25 cycle/sample
    (the widest occupancy of any waveform); MATH.md asks for -60 dB."""
    rng = np.random.default_rng(1)
    mu = np.concatenate([[0.0, 1.0, 1.0 - 2.0**-53, 2.0**-60], rng.random(3000)])
    w = signals._interp_weights(mu)
    assert w.shape == (mu.size, 16)
    offsets = np.arange(-7, 9)
    worst = 0.0
    for f in np.linspace(0.0, 0.25, 41):
        est = w @ np.exp(2j * np.pi * f * offsets)
        worst = max(worst, float(np.max(np.abs(est - np.exp(2j * np.pi * f * mu)))))
    assert 20 * math.log10(worst) < -100.0
    # exact at the grid points and continuous into the next interval
    np.testing.assert_array_equal(w[0], (offsets == 0).astype(float))
    np.testing.assert_array_equal(w[1], (offsets == 1).astype(float))


@pytest.mark.parametrize("name", ["noise", "qpsk", "psk_rolloff_1"])
def test_between_grid_points_matches_bandlimited_interpolation(name):
    """Off-grid values against trigonometric interpolation of the windowed grid
    samples (a non-uniform inverse DFT, exact for a band-limited periodic signal)."""
    w = {
        "noise": NoiseWaveform(KEY, 25e3),
        "qpsk": PskWaveform(KEY, 25_000, 4, 0.35),
        "psk_rolloff_1": PskWaveform(KEY, 25_000, 2, 1.0),
    }[name]
    rate = w.grid_rate_hz  # 100 kHz in all three cases: 10 000 ns per grid sample
    assert rate == 100_000
    n = 4096
    k0 = T0_NS // 10_000 + 1234
    g = w.grid_samples(k0, n)
    window = np.sin(np.pi * np.arange(n) / n) ** 2
    spec = np.fft.fft(window * g)
    rng = np.random.default_rng(2)
    x = rng.uniform(64.0, n - 64.0, 400)  # off-grid positions, in grid samples
    ref = np.exp(2j * np.pi * np.outer(x, np.fft.fftfreq(n))) @ spec / n
    got = np.sin(np.pi * x / n) ** 2 * w.evaluate(k0 * 10_000, x / rate)
    assert _err_db(got, ref) < -80.0


@pytest.mark.parametrize("name", ["noise", "qpsk"])
def test_two_sample_grids_agree_through_bandlimited_resampling(name):
    """The same waveform sampled at 40 kHz and at 64 kHz over the same 0.2 s."""
    w = _waveforms()[name]
    base = T0_NS + 55_555
    n1, n2, span = 8000, 12800, 0.2
    t1 = np.arange(n1) / 40e3
    t2 = np.arange(n2) / 64e3
    # A Hann taper makes the stretch periodic and smooth, so FFT resampling is exact.
    x1 = np.sin(np.pi * t1 / span) ** 2 * w.evaluate(base, t1)
    x2 = np.sin(np.pi * t2 / span) ** 2 * w.evaluate(base, t2)
    assert _err_db(_fft_resample(x1, n2), x2) < -50.0
    assert _err_db(_fft_resample(x2, n1), x1) < -50.0
    # sensitivity: a 1 microsecond timing slip (2.5 % of a sample) is far above the floor
    slipped = np.sin(np.pi * t2 / span) ** 2 * w.evaluate(base, t2 + 1e-6)
    assert _err_db(_fft_resample(x1, n2), slipped) > -40.0


# ----------------------------------------------------------- power and spectrum


def test_noise_has_unit_power_and_a_flat_band_limited_spectrum():
    bw = 25e3
    w = NoiseWaveform(KEY, bw)
    fs = 2.5 * bw
    y = w.evaluate(T0_NS, np.arange(163_840) / fs)
    assert abs(np.mean(np.abs(y) ** 2) - 1.0) < 0.02  # B T = 65 000: sigma 0.4 %
    # circular: no power in the pseudo-covariance
    assert abs(np.mean(y * y)) < 0.02
    f, p = _psd(y, fs, 4096)
    inband = np.abs(f) < 0.45 * bw
    level = p[inband].mean()
    assert abs(level / (fs / bw) - 1.0) < 0.03  # density 1 / bw over the band
    for chunk in np.array_split(p[inband], 8):  # flat: each eighth within 5 %
        assert abs(chunk.mean() / level - 1.0) < 0.05
    assert abs(_occupied_bw(f, p) / bw - 0.985) < 0.02
    assert 10 * np.log10(p[np.abs(f) > 0.52 * bw].max() / level) < -75.0
    # shape used by the fast spectrum path agrees with the measured spectrum
    shape = w.spectral_shape(f) * fs
    assert abs(np.mean(p[inband]) / np.mean(shape[inband]) - 1.0) < 0.03
    assert np.all(shape[np.abs(f) > 0.51 * bw] == 0.0)


def _raised_cosine(freq: np.ndarray, rs: float, rolloff: float) -> np.ndarray:
    """Textbook raised-cosine spectrum, 1 at the centre (area rs)."""
    x = np.abs(freq)
    f1, f2 = 0.5 * (1 - rolloff) * rs, 0.5 * (1 + rolloff) * rs
    roll = 0.5 * (1 + np.cos(np.pi * (np.clip(x, f1, f2) - f1) / (rolloff * rs)))
    return np.where(x <= f1, 1.0, np.where(x < f2, roll, 0.0))


@pytest.mark.parametrize("order,rolloff", [(4, 0.35), (2, 0.2), (4, 1.0)])
def test_psk_has_unit_power_and_a_raised_cosine_spectrum(order, rolloff):
    rs = 20_000
    w = PskWaveform(KEY, rs, order, rolloff)
    assert w.bandwidth_hz == pytest.approx(rs * (1 + rolloff))
    edge = 0.5 * w.bandwidth_hz

    # 1. The pulse: |H(f)|^2 of the shaping FIR by direct DTFT is the raised cosine
    #    (4 grid samples per symbol, so |H(0)| = 4).
    probe = np.linspace(0.0, 1.5 * edge, 301)
    j = np.arange(w.fir.size)
    resp = np.abs(np.exp(-2j * np.pi * np.outer(probe / w.grid_rate_hz, j)) @ w.fir) ** 2 / 16.0
    np.testing.assert_allclose(resp, _raised_cosine(probe, rs, rolloff), rtol=0, atol=1e-3)
    assert 10 * np.log10(resp[probe > 1.02 * edge].max()) < -90.0

    # 2. The rendered signal, at a rate unrelated to the symbol rate or the grid.
    fs = 4.0 * rs * 1.37
    y = w.evaluate(T0_NS, np.arange(262_144) / fs)
    assert abs(np.mean(np.abs(y) ** 2) - 1.0) < 0.01
    f, p = _psd(y, fs, 2048)
    p = p / fs  # density, 1/Hz; integrates to the mean power
    centre = p[np.abs(f) < 0.05 * rs].mean()
    # band averages against the raised cosine (the estimate scatters by about 2 %)
    bands = np.linspace(-edge, edge, 13)
    for lo, hi in zip(bands[:-1], bands[1:]):
        sel = (f >= lo) & (f < hi)
        assert abs(p[sel].mean() * rs - _raised_cosine(f[sel], rs, rolloff).mean()) < 0.08
    # half power at +/- rs / 2 (the roll-off is antisymmetric about that point)
    half = p[np.abs(np.abs(f) - 0.5 * rs) < 0.1 * rs].mean() * rs
    assert abs(half - 0.5) < 0.05
    # occupied bandwidth against the 99 % width of the raised cosine itself
    fine = np.linspace(-rs, rs, 200_001)
    theory = _occupied_bw(fine, _raised_cosine(fine, rs, rolloff))
    assert abs(_occupied_bw(f, p) / theory - 1.0) < 0.03
    assert 10 * np.log10(p[np.abs(f) > 1.03 * edge].max() / centre) < -70.0

    # 3. spectral_shape is that raised cosine, with unit area on the bin grid.
    shape = w.spectral_shape(f)
    assert abs(np.sum(shape) * (fs / 2048) - 1.0) < 1e-9
    scattered = np.sort(np.random.default_rng(6).uniform(-rs, rs, 500))  # point density
    np.testing.assert_allclose(
        w.spectral_shape(scattered) * rs, _raised_cosine(scattered, rs, rolloff), rtol=0, atol=1e-12
    )


def test_chirp_has_unit_envelope_and_fills_the_sweep_width():
    bw, period = 25e3, 0.01
    w = ChirpWaveform(bw, period)
    fs = 8 * bw
    y = w.evaluate(T0_NS + 4_321, np.arange(81_920) / fs)  # 41 sweeps
    np.testing.assert_allclose(np.abs(y), 1.0, rtol=0, atol=1e-12)
    f, p = _psd(y, fs, 4096)
    assert abs(_occupied_bw(f, p) / bw - 1.0) < 0.05
    inband = np.abs(f) < 0.45 * bw
    assert abs(p[inband].mean() / (fs / bw) - 1.0) < 0.05  # flat density 1 / bw
    # the frequency fly-back leaves skirts, 30 dB down in total beyond 1.5 x the width
    assert 10 * np.log10(p[np.abs(f) > 0.75 * bw].sum() / p.sum()) < -30.0
    assert w.bandwidth_hz == bw and w.sweep_period_s == pytest.approx(period)


def test_cw_is_constant_one():
    w = CwWaveform()
    y = w.evaluate(T0_NS, np.linspace(-1.0, 1.0, 1001))
    np.testing.assert_array_equal(y, np.ones(1001, dtype=np.complex128))
    assert w.bandwidth_hz == 0.0 and w.rms_bandwidth_hz == 0.0


@pytest.mark.parametrize("offset_bins", [0.0, 0.3, -0.5, 7.25])
def test_cw_spectral_shape_is_the_welch_response_to_a_tone(offset_bins):
    """The line is reported as the Hann kernel that welch_psd_db actually produces."""
    nfft, fs = 1024, 51_200.0
    df = fs / nfft
    f0 = offset_bins * df
    tone = np.exp(2j * np.pi * f0 * np.arange(nfft * 4) / fs)
    f, p = _psd(tone, fs, nfft)
    shape = CwWaveform().spectral_shape(f - f0) * df  # fraction of the power per bin
    assert abs(np.sum(shape) - 1.0) < 1e-4
    near = np.abs(f - f0) < 30 * df
    np.testing.assert_allclose(shape[near], p[near] / nfft, rtol=2e-2, atol=1e-9)
    if offset_bins == 0.0:
        k = nfft // 2
        np.testing.assert_allclose(shape[k - 1 : k + 2], [1 / 6, 2 / 3, 1 / 6], rtol=1e-12)
    # explicit resolution, any frequencies
    one = CwWaveform().spectral_shape(np.array([0.0]), resolution_hz=df)
    assert one[0] * df == pytest.approx(2 / 3)
    with pytest.raises(ValueError, match="freqs_hz"):
        CwWaveform().spectral_shape(np.array([0.0]))


def test_spectral_shape_integrates_to_one_however_narrow_the_signal():
    """Bin-averaged density: the sum over PSD bins is the in-span power fraction."""
    nfft, span = 1024, 1.0e6  # 977 Hz bins
    f = (np.arange(nfft) - nfft // 2) * span / nfft
    df = span / nfft
    for w in (
        NoiseWaveform(KEY, 300.0),  # a third of a bin
        NoiseWaveform(KEY, 25e3),
        ChirpWaveform(40e3, 0.01),
        PskWaveform(KEY, 100, 4, 0.35),
        PskWaveform(KEY, 20_000, 2, 0.0),
    ):
        for centre in (0.0, 12_345.6, -333_333.3):
            assert np.sum(w.spectral_shape(f - centre)) * df == pytest.approx(1.0, abs=1e-9)
    # half of a wide signal inside the span: half of the power
    wide = NoiseWaveform(KEY, 400e3)
    assert np.sum(wide.spectral_shape(f - (f[-1] + 0.5 * df))) * df == pytest.approx(0.5, abs=1e-9)
    # without a resolution it is the point density
    np.testing.assert_allclose(
        NoiseWaveform(KEY, 25e3).spectral_shape(np.array([0.0, 12_499.0, 12_501.0, -3e4])),
        [1 / 25e3, 1 / 25e3, 0.0, 0.0],
    )
    with pytest.raises(ValueError, match="resolution_hz"):
        wide.spectral_shape(f, resolution_hz=0.0)
    with pytest.raises(ValueError, match="freqs_hz"):
        wide.spectral_shape(np.array([0.0, np.nan]))


@pytest.mark.parametrize("name", ["chirp", "noise", "bpsk", "qpsk"])
def test_rms_bandwidth_matches_the_power_of_the_derivative(name):
    """integral f^2 S(f) df = <|ds/dt|^2> / (2 pi)^2 (Parseval), by finite differences."""
    w = _waveforms()[name]
    fs = 64.0 * w.bandwidth_hz
    y = w.evaluate(T0_NS, np.arange(400_000) / fs)
    deriv = (y[2:] - y[:-2]) * (fs / 2.0)
    measured = math.sqrt(float(np.mean(np.abs(deriv) ** 2) / np.mean(np.abs(y[1:-1]) ** 2))) / (
        2.0 * math.pi
    )
    assert abs(measured / w.rms_bandwidth_hz - 1.0) < 0.03
    if name == "chirp":
        assert w.rms_bandwidth_hz == pytest.approx(w.bandwidth_hz / math.sqrt(12.0))
    if name == "noise":  # a little under the ideal brick wall: the roll-off is inside the band
        assert 0.985 < w.rms_bandwidth_hz / (w.bandwidth_hz / math.sqrt(12.0)) < 1.0


def test_psk_rms_bandwidth_closed_form_against_numerical_integration():
    for rolloff in (0.0, 0.2, 0.35, 1.0):
        w = PskWaveform(KEY, 1000, 4, rolloff)
        f = np.linspace(-1000.0, 1000.0, 400_001)
        s = w.spectral_shape(f, resolution_hz=None) if rolloff else None
        if s is None:  # brick wall: closed form 1 / sqrt(12)
            assert w.rms_bandwidth_hz == pytest.approx(1000 / math.sqrt(12.0))
            continue
        num = math.sqrt(float(np.sum(f * f * s) / np.sum(s)))
        assert w.rms_bandwidth_hz == pytest.approx(num, rel=1e-6)


# ------------------------------------------------------------------------- chirp


def test_chirp_instantaneous_frequency_is_the_sawtooth():
    bw, period_ns = 25e3, 10_000_000
    w = ChirpWaveform(bw, period_ns * 1e-9)
    fs = 1.0e6  # 1 microsecond steps
    base = T0_NS + 3_333_500  # 3.3335 ms into a sweep (T0_NS is a multiple of 10 ms)
    assert T0_NS % period_ns == 0
    n = 30_000  # three sweeps
    y = w.evaluate(base, np.arange(n) / fs)
    inst = np.angle(y[1:] * np.conj(y[:-1])) * fs / (2 * np.pi)
    # time into the sweep at the midpoint of each step, in exact integer nanoseconds
    u_mid_ns = (3_333_500 + 1000 * np.arange(n - 1) + 500) % period_ns
    wraps = u_mid_ns < 1000 * 0.5 + 1  # steps that straddle the fly-back
    wraps |= u_mid_ns > period_ns - 501
    expected = -0.5 * bw + bw * u_mid_ns / period_ns
    np.testing.assert_allclose(inst[~wraps], expected[~wraps], rtol=0, atol=1e-4)
    assert expected[~wraps].min() < -0.499 * bw and expected[~wraps].max() > 0.499 * bw
    # phase is continuous through the fly-back: the step there is as small as any other
    assert np.max(np.abs(np.diff(y))) < 2 * np.pi * 0.5 * bw / fs * 1.01
    # the sweep restarts at multiples of the period since the epoch, with zero phase
    np.testing.assert_allclose(w.evaluate(T0_NS, np.array([0.0, 0.01, 0.02])), 1.0, atol=1e-9)


def test_chirp_phase_closed_form():
    bw, period = 40e3, 0.004
    w = ChirpWaveform(bw, period)
    u = np.linspace(0.0, period, 801)[:-1]
    expected = np.exp(1j * np.pi * bw * u * (u - period) / period)
    np.testing.assert_allclose(w.evaluate(T0_NS, u), expected, rtol=0, atol=1e-9)
    np.testing.assert_allclose(w.evaluate(T0_NS + 4_000_000 * 7, u), expected, rtol=0, atol=1e-9)


# --------------------------------------------------------------------------- psk


def test_psk_symbols_come_from_the_block_generator():
    """a[n] = signs of the normals of generator element n - 512 (block (n-512) // 4096)."""
    for order in (2, 4):
        w = PskWaveform(KEY, 20_000, order, 0.35)
        n_first = 4096 * 9_000_000 + 512 - 5  # straddles a generator block edge
        got = w.symbols(n_first, 10)
        for i in range(10):
            c = n_first + i - 512
            x = _documented_normals(KEY, c // 4096)
            m = c % 4096
            if order == 2:
                expected = complex(np.sign(x[2 * m]))
            else:
                expected = (np.sign(x[2 * m]) + 1j * np.sign(x[2 * m + 1])) / np.sqrt(2.0)
            assert got[i] == expected
    # balanced constellation
    s = PskWaveform(KEY, 20_000, 4, 0.35).symbols(0, 40_000)
    points = np.array([1 + 1j, 1 - 1j, -1 + 1j, -1 - 1j]) / np.sqrt(2)
    counts = [np.mean(np.isclose(s, v)) for v in points]
    assert sum(counts) == pytest.approx(1.0) and max(abs(c - 0.25) for c in counts) < 0.01
    assert abs(np.mean(s[1:] * np.conj(s[:-1]))) < 0.02


@pytest.mark.parametrize("order,rolloff", [(4, 0.35), (2, 0.5), (4, 1.0)])
def test_psk_symbols_are_recovered_at_the_symbol_instants(order, rolloff):
    """Matched filter (an independent textbook RRC) sampled at n / symbol_rate."""
    rs = 20_000  # 50 000 ns per symbol
    w = PskWaveform(KEY, rs, order, rolloff)
    sps, n_sym, span = 8, 400, 24
    n0 = (T0_NS // 50_000) + 77
    y = w.evaluate(n0 * 50_000, np.arange(n_sym * sps) / (sps * rs))
    t = np.arange(-span * sps, span * sps + 1) / sps

    def textbook_rrc(ts: np.ndarray, a: float) -> np.ndarray:
        ts = ts + 1e-9  # step off the removable singularities of this form
        num = np.sin(np.pi * ts * (1 - a)) + 4 * a * ts * np.cos(np.pi * ts * (1 + a))
        return num / (np.pi * ts * (1 - (4 * a * ts) ** 2))

    z = np.convolve(y, textbook_rrc(t, rolloff) / sps, mode="same")[::sps]
    ref = w.symbols(n0, n_sym)
    core = slice(span, n_sym - span)
    assert _rms(z[core] - ref[core]) < 0.01  # EVM below 1 %
    if order == 2:
        assert np.all(np.sign(z[core].real) == ref[core].real)
        assert np.max(np.abs(z[core].imag)) < 0.01
    else:
        assert np.all(np.sign(z[core].real) == np.sign(ref[core].real))
        assert np.all(np.sign(z[core].imag) == np.sign(ref[core].imag))
    # half a symbol off the true instants the eye is far from open
    y_off = w.evaluate(n0 * 50_000 + 25_000, np.arange(n_sym * sps) / (sps * rs))
    z_off = np.convolve(y_off, textbook_rrc(t, rolloff) / sps, mode="same")[::sps]
    assert _rms(z_off[core] - ref[core]) > 0.3


def test_rrc_pulse_matches_textbook_form_and_its_spectrum():
    for a in (0.2, 0.35, 1.0):
        t = np.linspace(-9.73, 9.91, 393)  # avoids 0 and +/- 1 / (4 a)
        num = np.sin(np.pi * t * (1 - a)) + 4 * a * t * np.cos(np.pi * t * (1 + a))
        np.testing.assert_allclose(
            rrc_pulse(t, a), num / (np.pi * t * (1 - (4 * a * t) ** 2)), rtol=0, atol=1e-11
        )
        # the removable singularities: t = 0 and t = 1 / (4 a)
        assert rrc_pulse(0.0, a) == pytest.approx(1 - a + 4 * a / np.pi, abs=1e-14)
        edge = (a / np.sqrt(2)) * (
            (1 + 2 / np.pi) * np.sin(np.pi / (4 * a)) + (1 - 2 / np.pi) * np.cos(np.pi / (4 * a))
        )
        assert rrc_pulse(1 / (4 * a), a) == pytest.approx(edge, abs=1e-13)
        # inverse Fourier transform of sqrt(raised cosine) by quadrature
        f = np.linspace(-(1 + a) / 2, (1 + a) / 2, 20_001)
        x = np.abs(f)
        rc = np.where(x <= (1 - a) / 2, 1.0, 0.5 * (1 + np.cos(np.pi / a * (x - (1 - a) / 2))))
        for ti in (0.0, 0.4, 1.0, 1 / (4 * a), 3.3):
            integrand = np.sqrt(rc) * np.cos(2 * np.pi * f * ti)
            quad = float(np.sum(integrand[1:] + integrand[:-1]) * (f[1] - f[0]) / 2)  # trapezoid
            assert rrc_pulse(ti, a) == pytest.approx(quad, abs=1e-7)
    np.testing.assert_allclose(rrc_pulse(np.array([0.0, 0.5, 2.0]), 0.0), np.sinc([0.0, 0.5, 2.0]))


# ------------------------------------------------------------------------ gating


def test_gate_is_on_for_the_first_duty_of_each_period():
    period_ns, duty = 2_000_000, 0.25
    w = CwWaveform(duty=duty, period_s=period_ns * 1e-9)
    assert T0_NS % period_ns == 0
    offset_ns = 300_500  # half a microsecond off the sample grid: no sample on an edge
    n = 20_000  # 1 microsecond steps, ten periods
    y = w.evaluate(T0_NS + offset_ns, np.arange(n) * 1e-6)
    on_expected = (offset_ns + 1000 * np.arange(n)) % period_ns < duty * period_ns
    np.testing.assert_array_equal(np.abs(y) > 0, on_expected)
    np.testing.assert_array_equal(y[on_expected], 1.0)
    assert np.mean(np.abs(y) ** 2) == pytest.approx(duty, abs=1e-4)
    np.testing.assert_array_equal(w.gate(T0_NS + offset_ns, np.arange(n) * 1e-6), on_expected)
    assert w.gate_period_s == pytest.approx(0.002) and w.duty == duty
    assert CwWaveform().gate_period_s is None
    assert np.all(CwWaveform().gate(T0_NS, np.arange(5.0)))


@pytest.mark.parametrize("name", ["noise", "qpsk", "chirp"])
def test_gated_waveform_is_the_ungated_one_times_the_gate(name):
    plain = _waveforms()[name]
    gated = {
        "noise": NoiseWaveform(KEY, 25e3, duty=0.3, period_s=0.004),
        "qpsk": PskWaveform(KEY, 18519, 4, 0.35, duty=0.3, period_s=0.004),
        "chirp": ChirpWaveform(25e3, 0.01, duty=0.3, period_s=0.004),
    }[name]
    t = (np.arange(40_000) + 0.5) / 100e3  # 0.4 s = 100 gate periods
    base = T0_NS + 250
    y, ref = gated.evaluate(base, t), plain.evaluate(base, t)
    on = gated.gate(base, t)
    assert abs(np.mean(on) - 0.3) < 1e-3
    np.testing.assert_allclose(y[on], ref[on], rtol=0, atol=1e-12)
    assert np.all(y[~on] == 0.0)
    # unit power while on, duty overall
    assert abs(np.mean(np.abs(y[on]) ** 2) - 1.0) < 0.08  # B T = 3000: sigma 1.8 %
    assert abs(np.mean(np.abs(y) ** 2) - 0.3) < 0.03
    # a gated waveform is still a pure function of absolute time
    shifted = gated.evaluate(base + 123_456_789, t - 0.123456789)
    assert np.mean(np.abs(shifted - y) > 1e-9) < 1e-3  # only samples within rounding of an edge


def test_gate_fraction_is_exact():
    w = NoiseWaveform(KEY, 25e3, duty=0.2, period_s=0.01)  # on 2 ms of every 10 ms
    assert T0_NS % 10_000_000 == 0
    cases = [
        (0, 0.01, 0.2),
        (0, 0.002, 1.0),
        (0, 0.001, 1.0),
        (2_000_000, 0.008, 0.0),
        (1_000_000, 0.002, 0.5),
        (9_000_000, 0.002, 0.5),
        (5_000_000, 0.105, 20 / 105),  # ten whole on-windows of 2 ms
        (1_500_000, 0.0, 1.0),
        (2_500_000, 0.0, 0.0),
    ]
    for offset_ns, duration, expected in cases:
        assert w.gate_fraction(T0_NS + offset_ns, duration) == pytest.approx(expected, abs=1e-9)
    # against brute force
    rng = np.random.default_rng(4)
    for _ in range(5):
        offset_ns = int(rng.integers(0, 10**7))
        duration = float(rng.uniform(1e-3, 5e-2))
        t = (np.arange(200_000) + 0.5) * (duration / 200_000)
        brute = np.mean(w.gate(T0_NS + offset_ns, t))
        assert w.gate_fraction(T0_NS + offset_ns, duration) == pytest.approx(brute, abs=1e-4)
    assert NoiseWaveform(KEY, 25e3).gate_fraction(T0_NS, 1.0) == 1.0
    with pytest.raises(ValueError, match="duration_s"):
        w.gate_fraction(T0_NS, -1.0)


# ------------------------------------------------------------ keys and correlation


def test_different_key_labels_give_uncorrelated_noise():
    t = np.arange(65_536) / 31_250.0
    noise = {"type": "noise", "bandwidth_hz": 25e3}
    a = waveform_from_spec(noise, "emitter|1|a|0").evaluate(T0_NS, t)
    b = waveform_from_spec(noise, "emitter|1|b|0").evaluate(T0_NS, t)
    c = waveform_from_spec(noise, "emitter|1|a|1").evaluate(T0_NS, t)
    np.testing.assert_array_equal(a, waveform_from_spec(noise, "emitter|1|a|0").evaluate(T0_NS, t))

    def rho(x: np.ndarray, y: np.ndarray) -> float:
        return float(abs(np.vdot(y, x)) / math.sqrt(np.vdot(x, x).real * np.vdot(y, y).real))

    # B T = 25 kHz x 2.1 s = 52 000 independent samples: sigma 0.0044
    assert rho(a, b) < 0.02 and rho(a, c) < 0.02 and rho(b, c) < 0.02
    # and at every lag within +/- 200 samples
    xc = np.fft.ifft(np.fft.fft(a) * np.conj(np.fft.fft(b))) / (a.size * _rms(a) * _rms(b))
    assert np.max(np.abs(np.concatenate([xc[:200], xc[-200:]]))) < 0.03
    # psk with different keys: different symbols
    psk = {"type": "psk", "bandwidth_hz": 25e3}
    p1 = waveform_from_spec(psk, "emitter|1|a|0").evaluate(T0_NS, t)
    p2 = waveform_from_spec(psk, "emitter|1|b|0").evaluate(T0_NS, t)
    assert rho(p1, p2) < 0.03


# --------------------------------------------------------------- block boundaries


class _NoHistoryNoise(NoiseWaveform):
    """Broken on purpose: every block is filtered on its own, as if nothing came
    before it, so the FIR does not carry across block edges."""

    def _build_blocks(self, blocks):
        out = {}
        for b in blocks:
            padded = np.concatenate([np.zeros(BLOCK, dtype=complex), raw_block(self.key, b)])
            out[b] = np.fft.ifft(np.fft.fft(padded) * np.fft.fft(self.fir, 2 * BLOCK))[BLOCK:]
        return out


def _out_of_band_db(w: NoiseWaveform, pieces: int = 1) -> float:
    bw = w.bandwidth_hz
    fs = 2.5 * bw  # 1.6 grid samples per output sample
    n = 163_840  # 64 blocks of the 100 kHz grid
    t = np.arange(n) / fs
    if pieces == 1:
        y = w.evaluate(T0_NS + 77, t)
    else:  # separate calls with their own base, 5 output samples = 80 000 ns exactly
        step = n // pieces
        assert step % 5 == 0
        y = np.concatenate(
            [w.evaluate(T0_NS + 77 + (i * step // 5) * 80_000, t[:step]) for i in range(pieces)]
        )
    f, p = _psd(y, fs, 4096)
    return float(10 * np.log10(p[np.abs(f) > 0.55 * bw].max() / p[np.abs(f) < 0.45 * bw].mean()))


def test_no_discontinuity_across_many_block_boundaries():
    """64 blocks in one call: any step at a block edge would splatter out of band."""
    w = NoiseWaveform(KEY, 25e3)
    assert _out_of_band_db(w) < -75.0
    assert w.blocks_built >= 64
    # the check has teeth: without the FIR history the edges show up 50 dB higher
    assert _out_of_band_db(_NoHistoryNoise(KEY, 25e3)) > -40.0


def test_no_discontinuity_across_calls_and_cache_evictions():
    small = NoiseWaveform(KEY, 25e3, cache_blocks=2)
    assert _out_of_band_db(small, pieces=16) < -75.0
    assert len(small._cache) <= 2
    psk = PskWaveform(KEY, 25_000, 4, 0.2)  # same 100 kHz grid
    fs = 80e3
    y = psk.evaluate(T0_NS + 5, np.arange(204_800) / fs)  # 62 blocks
    f, p = _psd(y, fs, 4096)
    edge = 0.5 * psk.bandwidth_hz
    out_of_band = p[np.abs(f) > 1.05 * edge].max() / p[np.abs(f) < 0.3 * edge].mean()
    assert 10 * np.log10(out_of_band) < -70.0


# --------------------------------------------------------------- cache and speed


def test_blocks_are_cached_per_waveform_object():
    w = NoiseWaveform(KEY, 1e6)
    t = np.arange(20_000) / 1.25e6
    w.evaluate(T0_NS, t)
    built = w.blocks_built
    assert 16 <= built <= 18  # 16 ms of a 4 MHz grid = 15.6 blocks, plus the edges
    again = w.evaluate(T0_NS, t)
    w.evaluate(T0_NS + 1_000_000, t - 0.0005)  # nearby: the other channel of a pair
    assert w.blocks_built <= built + 1
    np.testing.assert_array_equal(again, w.evaluate(T0_NS, t))
    other = NoiseWaveform(KEY, 1e6)
    assert other.blocks_built == 0  # the cache belongs to the object
    np.testing.assert_array_equal(other.evaluate(T0_NS, t), again)
    limited = NoiseWaveform(KEY, 1e6, cache_blocks=4)
    np.testing.assert_array_equal(limited.evaluate(T0_NS, t), again)
    assert len(limited._cache) <= 4
    # a long-lived waveform does not hold on to old captures: after a call 100 s later
    # only the blocks of that call remain, and going back regenerates the same values
    held = len(w._cache)
    w.evaluate(T0_NS + 100_000_000_000, t)
    assert len(w._cache) <= held + 1
    before = w.blocks_built
    np.testing.assert_array_equal(w.evaluate(T0_NS, t), again)
    assert w.blocks_built >= before + 16


def test_262144_noise_samples_render_quickly():
    """Target: about 0.3 s cold (measured 0.17 s cold, 0.06 s warm). The bounds are
    loose because the test machine is shared; they still catch a per-sample loop."""
    w = NoiseWaveform(KEY, 1e6)
    t = np.arange(262_144) / 1.25e6
    start = time.thread_time()  # CPU time of this thread: not inflated by a busy machine
    y = w.evaluate(T0_NS, t)
    cold = time.thread_time() - start
    built = w.blocks_built
    start = time.thread_time()
    w.evaluate(T0_NS, t)
    warm = time.thread_time() - start
    assert y.shape == (262_144,)
    assert 205 <= built <= 207 and w.blocks_built == built  # second call: no generation
    assert cold < 1.0
    assert warm < 0.5
    assert abs(np.mean(np.abs(y) ** 2) - 1.0) < 0.02


def test_concurrent_evaluation_is_consistent():
    w = NoiseWaveform(KEY, 200e3, cache_blocks=16)
    t = np.arange(30_000) / 250e3
    results: dict[int, np.ndarray] = {}

    def work(i: int) -> None:
        results[i] = w.evaluate(T0_NS + i * 4_000, t)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    fresh = NoiseWaveform(KEY, 200e3)
    for i in range(4):
        np.testing.assert_array_equal(results[i], fresh.evaluate(T0_NS + i * 4_000, t))


# ------------------------------------------------------------------- spec factory


class _Kind(enum.Enum):
    NOISE = "noise"


def test_waveform_from_spec_reads_objects_and_dicts():
    spec_obj = SimpleNamespace(
        type="noise", bandwidth_hz=25e3, sweep_period_s=0.01, symbol_rate_hz=0, order=4,
        rolloff=0.35, duty=1.0, period_s=0, seed=3,
    )
    a = waveform_from_spec(spec_obj, "emitter|1|x|3")
    b = waveform_from_spec({"type": "noise", "bandwidth_hz": 25e3}, "emitter|1|x|3")
    c = waveform_from_spec(SimpleNamespace(type=_Kind.NOISE, bandwidth_hz=25e3), "emitter|1|x|3")
    assert isinstance(a, NoiseWaveform) and a.kind == "noise" and a.bandwidth_hz == 25e3
    assert a.key == b.key == c.key == key128("emitter|1|x|3")
    t = np.arange(50) / 31250.0
    np.testing.assert_array_equal(a.evaluate(T0_NS, t), b.evaluate(T0_NS, t))
    np.testing.assert_array_equal(a.evaluate(T0_NS, t), c.evaluate(T0_NS, t))

    cw = waveform_from_spec({"type": "cw", "bandwidth_hz": 25e3}, "k")
    assert isinstance(cw, CwWaveform) and cw.bandwidth_hz == 0.0

    chirp = waveform_from_spec({"type": "chirp", "bandwidth_hz": 25e3}, "k")
    assert isinstance(chirp, ChirpWaveform) and chirp.sweep_period_s == pytest.approx(0.01)
    chirp = waveform_from_spec({"type": "chirp", "bandwidth_hz": 1e6, "sweep_period_s": 1e-3}, "k")
    assert chirp.sweep_period_s == pytest.approx(1e-3) and chirp.bandwidth_hz == 1e6

    # psk from a bandwidth: the largest integer symbol rate that fits
    psk = waveform_from_spec({"type": "psk", "bandwidth_hz": 25e3}, "k")
    assert isinstance(psk, PskWaveform)
    assert (psk.symbol_rate_hz, psk.order, psk.rolloff) == (18518, 4, 0.35)
    assert psk.bandwidth_hz <= 25e3 and psk.bandwidth_hz == pytest.approx(25e3, rel=1e-4)
    assert waveform_from_spec({"type": "psk", "bandwidth_hz": 27e3}, "k").symbol_rate_hz == 20_000
    # psk from a symbol rate
    psk = waveform_from_spec(
        {"type": "psk", "symbol_rate_hz": 16000.0, "order": 2, "rolloff": 0.5,
         "bandwidth_hz": 25e3},
        "k",
    )
    assert (psk.symbol_rate_hz, psk.order, psk.rolloff) == (16000, 2, 0.5)
    assert psk.bandwidth_hz == pytest.approx(24e3)

    gated = waveform_from_spec({"type": "cw", "duty": 0.1, "period_s": 0.05}, "k")
    assert gated.duty == 0.1 and gated.gate_period_s == pytest.approx(0.05)
    # duty 1 means not gated, whatever period_s says
    ungated = waveform_from_spec({"type": "cw", "duty": 1.0, "period_s": 0.05}, "k")
    assert ungated.gate_period_s is None


@pytest.mark.parametrize(
    "spec,field",
    [
        ({}, "type"),
        ({"type": "fm", "bandwidth_hz": 1e3}, "type"),
        ({"type": "noise"}, "bandwidth_hz"),
        ({"type": "noise", "bandwidth_hz": 0.5}, "bandwidth_hz"),
        ({"type": "noise", "bandwidth_hz": -1.0}, "bandwidth_hz"),
        ({"type": "noise", "bandwidth_hz": float("nan")}, "bandwidth_hz"),
        ({"type": "noise", "bandwidth_hz": "wide"}, "bandwidth_hz"),
        ({"type": "chirp", "bandwidth_hz": 0.0}, "bandwidth_hz"),
        ({"type": "chirp", "bandwidth_hz": 1e3, "sweep_period_s": 0.0}, "sweep_period_s"),
        ({"type": "chirp", "bandwidth_hz": 1e3, "sweep_period_s": 1e-10}, "sweep_period_s"),
        ({"type": "psk"}, "bandwidth_hz"),
        ({"type": "psk", "bandwidth_hz": 1.0}, "bandwidth_hz"),
        ({"type": "psk", "symbol_rate_hz": 1000.5}, "symbol_rate_hz"),
        ({"type": "psk", "symbol_rate_hz": -5}, "symbol_rate_hz"),
        ({"type": "psk", "symbol_rate_hz": 20_000, "bandwidth_hz": 20e3}, "bandwidth_hz"),
        ({"type": "psk", "bandwidth_hz": 1e4, "order": 8}, "order"),
        ({"type": "psk", "bandwidth_hz": 1e4, "rolloff": 1.5}, "rolloff"),
        ({"type": "psk", "bandwidth_hz": 1e4, "rolloff": -0.1}, "rolloff"),
        ({"type": "cw", "duty": 0.0}, "duty"),
        ({"type": "cw", "duty": 1.2}, "duty"),
        ({"type": "cw", "duty": 0.5}, "period_s"),
        ({"type": "cw", "duty": 0.5, "period_s": -1.0}, "period_s"),
    ],
)
def test_invalid_specs_raise_value_error_naming_the_field(spec, field):
    with pytest.raises(ValueError, match=field):
        waveform_from_spec(spec, "k")


def test_invalid_evaluate_arguments_raise_value_error():
    w = NoiseWaveform(KEY, 25e3)
    t = np.arange(4) / 31250.0
    with pytest.raises(ValueError, match="base_ns"):
        w.evaluate(1.79e18, t)  # float absolute time loses 240 ns
    with pytest.raises(ValueError, match="base_ns"):
        w.evaluate(True, t)
    with pytest.raises(ValueError, match="t_rel"):
        w.evaluate(T0_NS, np.array([0.0, np.nan]))
    with pytest.raises(ValueError, match="t_rel"):
        w.evaluate(T0_NS, np.array([0.0, 1e300]))
    with pytest.raises(ValueError, match="label"):
        waveform_from_spec({"type": "cw"}, 5)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="cache_blocks"):
        NoiseWaveform(KEY, 25e3, cache_blocks=-1)
    with pytest.raises(ValueError, match="count"):
        w.grid_samples(0, -1)
    assert w.evaluate(np.int64(T0_NS), t).shape == (4,)  # numpy integers are fine
    assert w.evaluate(T0_NS, np.empty(0)).shape == (0,)
