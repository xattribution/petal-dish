"""Independent verification of core/signals.py and core/channel.py (MATH.md section 4,
section 12 item 7).

Nothing here uses the implementation as its own reference. The references are:

- the generator of MATH.md written out again (Philox counter layout, pairing of normals);
- exact rational arithmetic (`fractions.Fraction`) for every absolute instant, so a
  240 ns float64 slip on absolute time would show;
- a time-domain convolution of those raw samples with the published FIR taps, followed
  by a 128-tap Kaiser-windowed-sinc resampler (the implementation uses 16 taps and a
  polyphase table);
- a direct sum of textbook root-raised-cosine pulses for psk;
- brute-force filtering for the decimator, and plain FFT spectra with a 150 dB window
  for leakage.

Negative controls show that each measurement would see the error it is looking for.
"""
from __future__ import annotations

import copy
import hashlib
import math
import pickle
import sys
from fractions import Fraction
from functools import lru_cache

import numpy as np
import pytest

from petal_locate.core.channel import (
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
    Waveform,
    key128,
    raw_block,
    rrc_pulse,
    waveform_from_spec,
)

BLK = 4096
LABEL = "emitter|7|verify|0"
KEY = int.from_bytes(hashlib.blake2b(LABEL.encode(), digest_size=16).digest(), "little")
T0_NS = 1_790_000_000_000_000_000  # the epoch region of MATH.md section 12 item 7
NS = 10**9


# ------------------------------------------------------------------ references


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.abs(x) ** 2)))


def _db(ratio: float) -> float:
    return 20.0 * math.log10(max(ratio, 1e-300))


@lru_cache(maxsize=256)
def _doc_normals(key: int, block: int) -> np.ndarray:
    """MATH.md section 4: the 8192 normals of generator block `block`."""
    counter = np.array([0, block % 2**64, 0, 0], dtype=np.uint64)
    return np.random.Generator(np.random.Philox(key=key, counter=counter)).standard_normal(8192)


def _doc_raw(key: int, block: int) -> np.ndarray:
    """Raw complex grid samples k = 4096 * block + m: (x[2m] + 1j x[2m+1]) / sqrt(2)."""
    x = _doc_normals(key, block)
    m = np.arange(BLK)
    return (x[2 * m] + 1j * x[2 * m + 1]) / math.sqrt(2.0)


def _grid_from_scratch(h: np.ndarray, key: int, k_lo: int, count: int) -> np.ndarray:
    """g[k_lo + i] = sum_j h[j] raw[k_lo + i - j] by time-domain convolution."""
    first = k_lo - (len(h) - 1)
    b0, b1 = first // BLK, (k_lo + count - 1) // BLK
    raw = np.concatenate([_doc_raw(key, b) for b in range(b0, b1 + 1)])
    seg = raw[first - b0 * BLK : k_lo + count - b0 * BLK]
    return np.convolve(seg, h, mode="valid")


def _exact_position(base_ns: int, t: np.ndarray, rate: int) -> tuple[list[int], np.ndarray]:
    """Grid position of the instants `base_ns * 1e-9 + t` as (integer part, fraction),
    computed in exact rational arithmetic from the float64 values of `t`."""
    whole: list[int] = []
    frac = np.empty(len(t))
    for i, ti in enumerate(t):
        p = Fraction(base_ns * rate, NS) + Fraction(float(ti)) * rate
        k = math.floor(p)
        whole.append(k)
        frac[i] = float(p - k)
    return whole, frac


def _sinc_weights(mu: np.ndarray, half: int, beta: float) -> np.ndarray:
    """Weights [n, 2 half] of grid samples k - half + 1 .. k + half for instants k + mu."""
    j = np.arange(-half + 1, half + 1, dtype=np.float64)
    d = j[None, :] - mu[:, None]
    window = np.i0(beta * np.sqrt(np.maximum(1.0 - (d / half) ** 2, 0.0))) / np.i0(beta)
    return np.sinc(d) * window


def _sinc_resample(
    g: np.ndarray, k_lo: int, whole: list[int], frac: np.ndarray, half: int = 64, beta: float = 20.0
) -> np.ndarray:
    """High-order band-limited interpolation of grid samples `g` (g[0] is grid index
    k_lo). 128 taps, Kaiser beta 20: flat to 0.44 cycles per sample, floor below -180 dB."""
    idx = np.array([k - k_lo for k in whole])[:, None] + np.arange(-half + 1, half + 1)[None, :]
    return np.einsum("ij,ij->i", _sinc_weights(frac, half, beta), g[idx])


def _noise_from_scratch(w: NoiseWaveform, key: int, base_ns: int, t: np.ndarray) -> np.ndarray:
    """s(base_ns * 1e-9 + t) from the documented generator, the FIR taps and a 128-tap
    sinc; each instant separately, so instants may be anywhere."""
    whole, frac = _exact_position(base_ns, t, w.grid_rate_hz)
    out = np.empty(len(t), dtype=np.complex128)
    half = 64
    for i, (k, mu) in enumerate(zip(whole, frac)):
        g = _grid_from_scratch(w.fir, key, k - half + 1, 2 * half)
        out[i] = np.dot(_sinc_weights(np.array([mu]), half, 20.0)[0], g)
    return out


def _textbook_rrc(t_sym: np.ndarray, a: float) -> np.ndarray:
    """Root-raised-cosine pulse in its usual closed form (unit energy), a > 0."""
    ts = np.asarray(t_sym, dtype=np.float64) + 1.37e-9  # step off the removable singularities
    num = np.sin(np.pi * ts * (1 - a)) + 4 * a * ts * np.cos(np.pi * ts * (1 + a))
    return num / (np.pi * ts * (1 - (4 * a * ts) ** 2))


def _psk_from_scratch(
    key: int, rs: int, order: int, a: float, base_ns: int, t: np.ndarray, span: int = 240
) -> np.ndarray:
    """sum_n a[n] p(t rs - n): symbol n is generator element n - 512 (signs of its two
    normals) and is centred on n / rs seconds since the Unix epoch."""
    whole, frac = _exact_position(base_ns, t, rs)
    out = np.empty(len(t), dtype=np.complex128)
    offs = np.arange(-span, span + 1)
    for i, (n0, mu) in enumerate(zip(whole, frac)):
        c = n0 + offs - 512
        blocks, elems = c // BLK, c % BLK
        re = np.empty(len(c))
        im = np.empty(len(c))
        for b in np.unique(blocks):
            x = _doc_normals(key, int(b))
            sel = blocks == b
            re[sel] = np.where(x[2 * elems[sel]] >= 0.0, 1.0, -1.0)
            im[sel] = np.where(x[2 * elems[sel] + 1] >= 0.0, 1.0, -1.0)
        sym = re.astype(np.complex128) if order == 2 else (re + 1j * im) / math.sqrt(2.0)
        out[i] = np.dot(sym, _textbook_rrc(mu - offs, a))
    return out


def _spectrum(
    y: np.ndarray, fs: float, nfft: int, beta: float = 20.0, hop: int | None = None
) -> tuple[np.ndarray, np.ndarray]:
    """Averaged periodogram with a Kaiser window (sidelobes below -150 dB for beta 20).
    Segments advance by `hop` samples (default nfft / 4, so no stretch of the record
    sits only under the window's skirts). Returns (frequency axis Hz, density 1/Hz
    integrating to the mean power), fft-shifted."""
    win = np.kaiser(nfft, beta)
    hop = nfft // 4 if hop is None else hop
    starts = np.arange(0, len(y) - nfft + 1, hop)
    seg = y[starts[:, None] + np.arange(nfft)[None, :]] * win
    p = np.mean(np.abs(np.fft.fft(seg, axis=1)) ** 2, axis=0) / (np.sum(win * win) * fs)
    return np.fft.fftshift(np.fft.fftfreq(nfft, 1.0 / fs)), np.fft.fftshift(p)


def _autocorr(x: np.ndarray) -> np.ndarray:
    """Normalised (biased) autocorrelation at lags 0..N-1, no circular wrap."""
    n = len(x)
    spec = np.fft.fft(x, 2 * n)
    r = np.fft.ifft(np.abs(spec) ** 2)[:n]
    return r / r[0].real


def _lag(x: np.ndarray, lag: int) -> float:
    """|sum x[n + lag] conj(x[n])| / sum |x|^2 by direct summation."""
    return float(abs(np.vdot(x[:-lag], x[lag:])) / np.vdot(x, x).real)


# ----------------------------------------------------------- keys and raw blocks


def test_key128_is_blake2b_16_little_endian():
    for label in ("emitter|1|jam|0", "relay|3|sat-a|x1|259000000|31250", "thermal|1|c|5|31250.0|2.59e8", ""):
        digest = hashlib.blake2b(label.encode(), digest_size=16).digest()
        assert key128(label) == sum(byte << (8 * i) for i, byte in enumerate(digest))
    assert key128(LABEL) == KEY
    assert waveform_from_spec({"type": "noise", "bandwidth_hz": 1e3}, LABEL).key == KEY  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "label,rate",
    [("emitter|7|verify|0", 100_000), ("relay|1|sat-a|x1", 144_000_000), ("emitter|1|b|9", 4)],
)
def test_item7_raw_blocks_white_at_every_lag(label, rate):
    """MATH.md 12.7, first half, on the blocks in use at the 2026 epoch for three grid
    rates; the module's raw_block must be the documented generator, bit for bit."""
    key = int.from_bytes(hashlib.blake2b(label.encode(), digest_size=16).digest(), "little")
    b0 = (T0_NS * rate // NS) // BLK
    mine = [_doc_raw(key, b0 + i) for i in range(64)]
    for i in (0, 1, 31, 63):
        np.testing.assert_array_equal(raw_block(key, b0 + i), mine[i])
    x = np.concatenate(mine)
    assert abs(np.mean(np.abs(x) ** 2) - 1.0) < 0.01
    assert abs(np.mean(x * x)) < 0.01  # circular
    r = np.abs(_autocorr(x))
    assert r[1:].max() < 0.02
    # the two lags MATH.md names, and their neighbours, by direct summation
    for lag in (1, 2, 4094, 4095, 4096, 8188, 8190, 8192):
        assert _lag(x, lag) < 0.02, lag
        assert abs(_lag(x, lag) - r[lag]) < 1e-12


def test_item7_measurement_sees_the_layout_it_guards_against():
    """Negative control: block index in the low counter word (the one the generator
    increments) makes block b + 1 a copy of block b advanced by two complex samples."""
    blocks = []
    for b in range(64):
        counter = np.array([b, 0, 0, 0], dtype=np.uint64)
        x = np.random.Generator(np.random.Philox(key=KEY, counter=counter)).standard_normal(8192)
        blocks.append((x[0::2] + 1j * x[1::2]) / math.sqrt(2.0))
    x = np.concatenate(blocks)
    assert _lag(x, 4094) > 0.9 and _lag(x, 8188) > 0.9


def test_item7_rendered_noise_has_no_echo_at_block_spacing():
    """The same property after the FIR and the interpolator: 64 blocks of the waveform
    itself, sampled on its grid through evaluate, have no correlation at 4094, 4096,
    8188 or 8192 grid samples (B T = 65 536, sigma 0.004), nor at any lag beyond the
    filter's own correlation length."""
    w = NoiseWaveform(KEY, 25e3)  # 100 kHz grid: 10 000 ns per grid sample
    k0 = (T0_NS // 10_000 // BLK) * BLK
    y = w.evaluate(k0 * 10_000, np.arange(64 * BLK) * 1e-5)
    r = np.abs(_autocorr(y))
    for lag in (4094, 4096, 8188, 8192):
        assert r[lag] < 0.02, lag
    assert r[4000:].max() < 0.025
    # at short lags it is the autocorrelation of a flat band 0.2472 grid rates wide
    # (the 0.25 of MATH.md less the filter's transition): sinc(0.2472 lag)
    for lag in (1, 2, 3, 4, 6):
        assert abs(r[lag] - abs(np.sinc(0.2472 * lag))) < 0.02, lag


# ----------------------------------------------- absolute time and the base/t_rel split


def _zoo() -> dict[str, Waveform]:
    return {
        "cw": CwWaveform(),
        "chirp": ChirpWaveform(25e3, 0.01),
        "noise": NoiseWaveform(KEY, 25e3),
        "noise_odd_rate": NoiseWaveform(KEY, 12_345.6),
        "bpsk": PskWaveform(KEY, 16_000, 2, 0.5),
        "qpsk": PskWaveform(KEY, 18_519, 4, 0.35),
    }


@pytest.mark.parametrize("bandwidth_hz", [25e3, 12_345.6, 400e3])
def test_item7_shift_by_123456789_ns(bandwidth_hz):
    """MATH.md 12.7, second half, for random b near 1.79e18."""
    rng = np.random.default_rng(20261113)
    w = NoiseWaveform(KEY, bandwidth_hz)
    for _ in range(6):
        b = 1_790_000_000_000_000_000 + int(rng.integers(-(10**16), 10**16))
        t = rng.uniform(0.0, 2000.0 / bandwidth_hz, 1500)
        a = w.evaluate(b, t)
        c = w.evaluate(b + 123_456_789, t - 0.123456789)
        assert np.max(np.abs(a - c)) <= 1e-9 * _rms(a)
        assert 0.8 < _rms(a) < 1.2


@pytest.mark.parametrize("name", ["cw", "chirp", "noise", "noise_odd_rate", "bpsk", "qpsk"])
def test_split_between_base_and_t_rel_does_not_matter_at_nanosecond_offsets(name):
    """All four waveform types, shifts of single nanoseconds up to seconds, for bases
    that are not multiples of anything."""
    w = _zoo()[name]
    rng = np.random.default_rng(5)
    for _ in range(3):
        base = T0_NS + int(rng.integers(0, 10**15))
        t = rng.uniform(-0.02, 0.06, 1200)
        ref = w.evaluate(base, t)
        assert ref.dtype == np.complex128
        scale = _rms(ref)
        for shift_ns in (1, -1, 2, 3, -7, 17, 999, -1001, 1_000_003, -123_456_789, 2_000_000_011):
            got = w.evaluate(base + shift_ns, t - shift_ns * 1e-9)
            assert np.max(np.abs(got - ref)) <= 1e-9 * scale, (name, shift_ns)


@pytest.mark.parametrize("name", ["cw", "chirp", "noise", "qpsk"])
def test_split_does_not_matter_for_gated_waveforms(name):
    """Gated versions: identical except within rounding (1e-15 s) of a gate edge, and
    the instants here are kept a nanosecond away from every edge."""
    period_ns, duty = 3_000_000, 0.4
    gated: Waveform = {
        "cw": CwWaveform(duty=duty, period_s=3e-3),
        "chirp": ChirpWaveform(25e3, 0.01, duty=duty, period_s=3e-3),
        "noise": NoiseWaveform(KEY, 25e3, duty=duty, period_s=3e-3),
        "qpsk": PskWaveform(KEY, 18_519, 4, 0.35, duty=duty, period_s=3e-3),
    }[name]
    rng = np.random.default_rng(6)
    base = T0_NS + 1_234_567
    t = rng.uniform(-0.02, 0.06, 3000)
    u_ns = np.array([float((Fraction(base) + Fraction(float(x)) * NS) % period_ns) for x in t])
    clear = (np.abs(u_ns - duty * period_ns) > 1.0) & (u_ns > 1.0) & (u_ns < period_ns - 1.0)
    t = t[clear]
    on_exact = u_ns[clear] < duty * period_ns
    ref = gated.evaluate(base, t)
    np.testing.assert_array_equal(ref != 0.0, on_exact)  # on for the first 40 % of each period
    assert 0.3 < np.mean(on_exact) < 0.5
    for shift_ns in (1, -1, 999, -123_456_789, 2_000_000_011):
        got = gated.evaluate(base + shift_ns, t - shift_ns * 1e-9)
        assert np.max(np.abs(got - ref)) <= 1e-9
    np.testing.assert_array_equal(gated.gate(base, t), on_exact)


@pytest.mark.parametrize(
    "bandwidth_hz,period_s", [(25e3, 0.01), (1e6, 0.000333333), (2.5e3, 0.25), (40e3, 0.004)]
)
def test_chirp_against_exact_rational_arithmetic(bandwidth_hz, period_s):
    """u = (absolute time) mod period and phase = pi B u (u - P) / P, evaluated with
    Fractions from integer nanoseconds: sweeps restart at multiples of the period since
    the Unix epoch, start at -B/2, and no float ever holds the absolute time."""
    w = ChirpWaveform(bandwidth_hz, period_s)
    period_ns = round(period_s * 1e9)
    period = Fraction(period_ns, NS)
    rng = np.random.default_rng(8)
    base = T0_NS + int(rng.integers(0, 10**13))
    t = rng.uniform(-0.3, 0.7, 500)
    ref = np.empty(t.size, dtype=np.complex128)
    freq = np.empty(t.size)
    for i, ti in enumerate(t):
        u = (Fraction(base, NS) + Fraction(float(ti))) % period
        cycles = Fraction(bandwidth_hz) * u * (u - period) / (2 * period)
        ref[i] = np.exp(2j * np.pi * float(cycles % 1))
        freq[i] = float(Fraction(bandwidth_hz) * (u / period - Fraction(1, 2)))
    got = w.evaluate(base, t)
    assert np.max(np.abs(got - ref)) < 1e-9
    np.testing.assert_allclose(np.abs(got), 1.0, rtol=0, atol=1e-12)
    # instantaneous frequency by a 1 ns two-sided difference (inside one sweep)
    inside = np.abs(freq) < 0.49 * bandwidth_hz
    d = w.evaluate(base, t[inside] + 1e-9) * np.conj(w.evaluate(base, t[inside] - 1e-9))
    measured = np.angle(d) / (2 * np.pi * 2e-9)
    assert np.max(np.abs(measured - freq[inside])) < 1e-6 * bandwidth_hz + 2.0
    # negative control: absolute seconds held in a float64 (240 ns resolution) are
    # nowhere near this tolerance, so the check above really resolves the timing
    u_naive = np.mod(float(base) * 1e-9 + t, period_s)
    naive = np.exp(1j * np.pi * bandwidth_hz * u_naive * (u_naive - period_s) / period_s)
    assert np.max(np.abs(naive - ref)) > 1e-4


def test_gate_fraction_against_exact_rational_arithmetic():
    rng = np.random.default_rng(9)
    for duty, period_s in ((0.2, 0.01), (0.37, 0.0123456), (0.9, 2.0)):
        w = NoiseWaveform(KEY, 5e3, duty=duty, period_s=period_s)
        period_ns = round(period_s * 1e9)
        period = Fraction(period_ns, NS)
        on = Fraction(duty) * period

        def on_time(x: Fraction) -> Fraction:
            cycles = math.floor(x / period)
            return cycles * on + min(x - cycles * period, on)

        for _ in range(8):
            base = T0_NS + int(rng.integers(0, 10**13))
            duration = float(rng.uniform(0.05 * period_s, 12.0 * period_s))
            start = Fraction(base % period_ns, NS)
            exact = float((on_time(start + Fraction(duration)) - on_time(start)) / Fraction(duration))
            assert w.gate_fraction(base, duration) == pytest.approx(exact, abs=1e-9)
            # and against the gate itself, sampled
            t = (np.arange(40_000) + 0.5) * (duration / 40_000)
            assert abs(np.mean(w.gate(base, t)) - exact) < 2e-3


# ------------------------------------------- noise and psk rebuilt from the documents


@pytest.mark.parametrize("bandwidth_hz", [25e3, 12_345.6, 3e6])
def test_noise_equals_documented_generator_fir_and_bandlimited_interpolation(bandwidth_hz):
    """evaluate against a from-scratch rebuild at exact rational instants: at the 2026
    epoch, around the Unix epoch (negative grid and block indices), minutes apart
    (sparse block sets), and within a few grid samples of block edges."""
    w = NoiseWaveform(KEY, bandwidth_hz, cache_blocks=3)
    rate = w.grid_rate_hz
    assert rate == math.ceil(4 * bandwidth_hz) and isinstance(rate, int)
    rng = np.random.default_rng(10)
    for base in (T0_NS + 17, 0, -5_000_000_000_123):
        q = base * rate // NS
        edges = ((q // BLK + rng.integers(-3, 4, 8)) * BLK - q + rng.uniform(-9.0, 9.0, 8)) / rate
        t = np.concatenate(
            [rng.uniform(-900.0, 900.0, 5), rng.uniform(-2e-3, 2e-3, 6), edges, [0.0, 1e-9, -1e-9]]
        )
        got = w.evaluate(base, t)
        ref = _noise_from_scratch(w, KEY, base, t)
        # 900 s * rate is known to 1 ulp only: allow that much time error on the far ones
        slack = 2.0 * np.spacing(np.abs(t) * rate) * 2 * np.pi * 0.125
        assert np.all(np.abs(got - ref) < 2e-5 + slack), (base, np.max(np.abs(got - ref)))


@pytest.mark.parametrize("order,rolloff,rs", [(4, 0.35, 20_000), (2, 0.5, 16_000), (4, 1.0, 18_519), (4, 0.2, 7)])
def test_psk_equals_direct_sum_of_textbook_rrc_pulses(order, rolloff, rs):
    """Symbols from the generator (element n - 512, signs of its normals), each centred
    on n / symbol_rate since the Unix epoch, shaped by the closed-form RRC pulse. The
    floor (-75 dB) is the 240-symbol truncation of this reference and the taper of the
    implementation's pulse; one grid sample of timing error would read -12 dB."""
    w = PskWaveform(KEY, rs, order, rolloff)
    rng = np.random.default_rng(11)
    for base in (T0_NS + int(rng.integers(0, 10**13)), -3_000_000_000_000 // rs):
        # symbols next to the edges of the generator's blocks (every 4096 elements, so
        # symbol 512 + 4096 j) and of the grid blocks (every 1024 symbols), then anywhere
        n_base = base * rs // NS
        j = rng.integers(-3, 9, 40)
        near_edges = 512 + 1024 * (n_base // 1024 + j) + rng.uniform(-3.0, 3.0, 40)
        t = np.concatenate([(near_edges - n_base) / rs, rng.uniform(-40.0 / rs, 2600.0 / rs, 220)])
        got = w.evaluate(base, t)
        ref = _psk_from_scratch(KEY, rs, order, rolloff, base, t)
        assert _db(_rms(got - ref) / _rms(ref)) < -75.0
        assert _db(float(np.max(np.abs(got - ref))) / _rms(ref)) < -65.0
        slipped = w.evaluate(base, t + 0.25 / rs)  # one grid sample late
        assert _db(_rms(slipped - ref) / _rms(ref)) > -20.0
    # unit mean power, over thousands of symbols at a rate unrelated to the symbol rate
    y = w.evaluate(T0_NS, np.arange(60_000) / (3.7 * rs))
    assert abs(np.mean(np.abs(y) ** 2) - 1.0) < 0.02


@pytest.mark.parametrize("name", ["noise", "noise_odd_rate", "bpsk_rolloff_1", "qpsk"])
def test_interpolation_error_against_128_tap_sinc(name):
    """Off-grid values against a 128-tap Kaiser sinc applied to the grid samples.
    MATH.md asks for -60 dB; the 16-tap kernel with its polyphase table measures
    about -117 dB rms and -105 dB worst case, including the widest occupancy
    (psk with roll-off 1: 0.25 cycle per grid sample)."""
    w = {
        "noise": NoiseWaveform(KEY, 25e3),
        "noise_odd_rate": NoiseWaveform(KEY, 12_345.6),
        "bpsk_rolloff_1": PskWaveform(KEY, 25_000, 2, 1.0),
        "qpsk": PskWaveform(KEY, 18_519, 4, 0.35),
    }[name]
    rate = w.grid_rate_hz
    rng = np.random.default_rng(12)
    base = T0_NS + int(rng.integers(0, 10**12))
    t = np.sort(rng.uniform(0.0, 3.2 * BLK / rate, 1500))  # three block edges inside
    whole, frac = _exact_position(base, t, rate)
    k_lo = min(whole) - 80
    count = max(whole) + 80 - k_lo
    if isinstance(w, NoiseWaveform):
        grid = _grid_from_scratch(w.fir, KEY, k_lo, count)  # nothing from the implementation
        assert np.max(np.abs(w.grid_samples(k_lo, count) - grid)) < 1e-12
    else:
        grid = w.grid_samples(k_lo, count)
    ref = _sinc_resample(grid, k_lo, whole, frac)
    err = w.evaluate(base, t) - ref
    assert _db(_rms(err) / _rms(ref)) < -105.0
    assert _db(float(np.max(np.abs(err))) / _rms(ref)) < -95.0
    # the reference itself: a second resampler of different length and window agrees
    # with it 40 dB below the errors being measured
    other = _sinc_resample(grid, k_lo, whole, frac, half=48, beta=16.0)
    assert _db(_rms(other - ref) / _rms(ref)) < -140.0
    # and a quarter of a grid sample of timing error would read about -10 dB
    late = w.evaluate(base, t + 0.25 / rate) - ref
    assert _db(_rms(late) / _rms(ref)) > -25.0


def test_evaluate_is_exact_on_grid_instants_and_continuous_across_them():
    w = NoiseWaveform(KEY, 25e3)  # 10 000 ns per grid sample
    k0 = ((T0_NS // 10_000) // BLK + 2) * BLK - 3  # run across a block edge
    grid = _grid_from_scratch(w.fir, KEY, k0, 6)
    for i in range(6):
        on = w.evaluate((k0 + i) * 10_000, np.array([0.0, 1e-13, -1e-13]))
        assert abs(on[0] - grid[i]) < 1e-13
        assert np.max(np.abs(on[1:] - grid[i])) < 1e-7  # 1e-8 grid sample either side


# ------------------------------------------------------------ FIR, power, spectrum


def test_noise_fir_is_short_unit_power_flat_and_band_limited():
    for bandwidth_hz in (25e3, 12_345.6):
        w = NoiseWaveform(KEY, bandwidth_hz)
        h = w.fir
        assert h.ndim == 1 and h.size <= 4097  # only adjacent blocks are needed
        assert abs(float(np.sum(h * h)) - 1.0) < 1e-12  # unit output power for unit white input
        resp = np.abs(np.fft.rfft(h, 1 << 19)) ** 2
        f = np.arange(resp.size) * (w.grid_rate_hz / (1 << 19))
        flat = resp[f <= 0.485 * bandwidth_hz]
        assert 10 * math.log10(flat.max() / flat.min()) < 0.01
        assert 10 * math.log10(resp[f >= 0.5 * bandwidth_hz].max() / flat.mean()) < -85.0
        # equivalent noise bandwidth: the in-band density is 1 / enbw, within 1.5 % of
        # the 1 / bandwidth_hz that spectral_shape reports
        enbw = w.grid_rate_hz * float(np.sum(h * h)) / float(np.sum(h)) ** 2
        assert 0.985 < enbw / bandwidth_hz < 1.0


@pytest.mark.parametrize("name", ["noise", "qpsk", "bpsk_rolloff_1", "chirp"])
def test_power_spectrum_and_rms_bandwidth_as_measured(name):
    """Mean power 1, the spectrum where spectral_shape says it is, nothing outside the
    stated bandwidth, and rms_bandwidth_hz equal to the second moment of the measured
    spectrum. Sampled at a rate unrelated to the grid."""
    w = {
        "noise": NoiseWaveform(KEY, 25e3),
        "qpsk": PskWaveform(KEY, 18_519, 4, 0.35),
        "bpsk_rolloff_1": PskWaveform(KEY, 12_500, 2, 1.0),
        "chirp": ChirpWaveform(25e3, 0.01),
    }[name]
    bw = w.bandwidth_hz
    fs = 6.4 * 1.0137 * bw
    y = w.evaluate(T0_NS + 99, np.arange(1 << 19) / fs)
    assert abs(np.mean(np.abs(y) ** 2) - 1.0) < 0.01
    nfft = 1024
    f, p = _spectrum(y, fs, nfft, beta=14.0)
    df = fs / nfft
    assert abs(np.sum(p) * df - 1.0) < 0.03
    measured_rms = math.sqrt(float(np.sum(f * f * p) / np.sum(p)))
    assert abs(measured_rms / w.rms_bandwidth_hz - 1.0) < 0.015
    shape = w.spectral_shape(f)
    assert abs(np.sum(shape) * df - 1.0) < 1e-9
    # band averages (eighths of the occupied band) against spectral_shape
    edges = np.linspace(-0.5 * bw, 0.5 * bw, 9)
    for lo, hi in zip(edges[:-1], edges[1:]):
        sel = (f >= lo) & (f < hi)
        tol = 0.12 if name == "chirp" else 0.06  # a chirp spectrum has Fresnel ripple
        assert abs(np.mean(p[sel]) - np.mean(shape[sel])) * bw < tol, (lo, hi)
    out_of_band = p[np.abs(f) > 0.5 * bw + 8 * df].max() / p[np.abs(f) < 0.2 * bw].mean()
    if name == "chirp":  # frequency fly-back: skirts, but 99 % of the power is inside
        assert np.sum(p[np.abs(f) <= 0.5 * bw]) * df > 0.97
    else:
        assert 10 * math.log10(out_of_band) < -80.0


def test_spectral_shape_is_the_density_averaged_over_each_bin():
    """On a uniform bin grid the value in bin k is the fraction of the power between
    f_k - df/2 and f_k + df/2, divided by df: closed-form overlap for the flat shapes,
    fine quadrature of the raised cosine for psk. Half a bin of misregistration would
    move a third of a one-bin-wide signal into the neighbouring bin."""
    nfft, span = 512, 64_000.0
    df = span / nfft  # 125 Hz
    f = (np.arange(nfft) - nfft // 2) * df
    for width, centre in ((125.0, 0.0), (125.0, 1000.0), (300.0, -4031.25), (10_000.0, 62.5), (90.0, 20.0)):
        lo = np.maximum(f - 0.5 * df, centre - 0.5 * width)
        hi = np.minimum(f + 0.5 * df, centre + 0.5 * width)
        expected = np.maximum(hi - lo, 0.0) / (width * df)
        for w in (NoiseWaveform(KEY, width), ChirpWaveform(width, 0.01)):
            np.testing.assert_allclose(w.spectral_shape(f - centre), expected, rtol=0, atol=1e-9 / df)
    one_bin = NoiseWaveform(KEY, 125.0).spectral_shape(f)  # exactly bin nfft/2, nothing else
    assert one_bin[nfft // 2] * df == pytest.approx(1.0, abs=1e-9)
    assert np.count_nonzero(one_bin > 1e-12) == 1

    rs, a, centre = 2_000, 0.5, 333.0
    psk = PskWaveform(KEY, rs, 4, a)
    fine = (np.arange(nfft * 400) + 0.5) * (df / 400) + (f[0] - 0.5 * df)  # 400 points per bin
    x = np.abs(fine - centre)
    f1, f2 = 0.5 * (1 - a) * rs, 0.5 * (1 + a) * rs
    rc = np.where(x <= f1, 1.0, np.where(x < f2, 0.5 * (1 + np.cos(np.pi * (x - f1) / (a * rs))), 0.0)) / rs
    np.testing.assert_allclose(
        psk.spectral_shape(f - centre), rc.reshape(nfft, 400).mean(axis=1), rtol=0, atol=2e-6 / rs
    )


@pytest.mark.parametrize("offset_bins", [0.0, 0.5, -0.25, 3.3])
def test_cw_spectral_shape_is_the_hann_periodogram_of_a_tone(offset_bins):
    """The cw line as the PSD estimator of MATH.md section 7 sees it: |FFT(w x)|^2 /
    sum(w^2) of a unit tone with the periodic Hann window, as a fraction of nfft."""
    nfft, fs = 512, 64_000.0
    df = fs / nfft
    f = (np.arange(nfft) - nfft // 2) * df
    f0 = offset_bins * df
    window = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(nfft) / nfft)
    tone = np.exp(2j * np.pi * f0 * np.arange(nfft) / fs)
    measured = np.fft.fftshift(np.abs(np.fft.fft(window * tone)) ** 2) / np.sum(window**2) / nfft
    shape = CwWaveform().spectral_shape(f - f0) * df  # fraction of the power per bin
    near = np.abs(f - f0) < 40 * df
    np.testing.assert_allclose(shape[near], measured[near], rtol=0, atol=2e-5)
    assert abs(np.sum(shape) - 1.0) < 1e-4
    assert int(np.argmax(shape)) == int(np.argmax(measured))


# ----------------------------------------------------- block boundaries: leakage


def _leakage_db(y: np.ndarray, fs: float, bandwidth_hz: float) -> float:
    """Highest out-of-band density (beyond 0.6 of the bandwidth) over the mean in-band
    density, dB. A step or a glitch at block edges is broadband and lands there."""
    f, p = _spectrum(y, fs, 4096)
    return 10.0 * math.log10(
        p[np.abs(f) > 0.6 * bandwidth_hz].max() / p[np.abs(f) < 0.4 * bandwidth_hz].mean()
    )


def _block_phase(base_ns: int, t: np.ndarray, rate: int) -> np.ndarray:
    """Position of each instant inside its generator block, grid samples in [0, 4096)."""
    q, r = divmod(base_ns * rate, NS)
    return np.mod((q % BLK) + r * 1e-9 + t * rate, BLK)


def _edge_excess_db(y: np.ndarray, fs: float, bandwidth_hz: float, phase: np.ndarray) -> float:
    """Out-of-band residual (brick-wall high-pass beyond 0.6 of the bandwidth, taken on a
    tapered record) near block edges over the same residual elsewhere, dB."""
    n = len(y)
    taper = np.kaiser(n, 20.0)
    spec = np.fft.fft(y * taper)
    spec[np.abs(np.fft.fftfreq(n, 1.0 / fs)) < 0.6 * bandwidth_hz] = 0.0
    resid = np.fft.ifft(spec)
    centre = slice(n // 4, 3 * n // 4)  # where the taper is near 1
    resid, phase = resid[centre], phase[centre]
    near = (phase < 24.0) | (phase > BLK - 24.0)
    return 20.0 * math.log10(_rms(resid[near]) / _rms(resid[~near]))


@pytest.mark.parametrize(
    "name,ratio",
    [("noise", 0.4), ("noise", 0.5173), ("noise", 1.0), ("noise", 2.31), ("psk", 0.8), ("psk", 1.37)],
)
def test_block_boundaries_leave_no_spectral_leakage(name, ratio):
    """48 generator blocks rendered at several ratios of the grid rate: the out-of-band
    floor is the interpolator's (measured -115 to -123 dB), and it is no higher next to
    block edges than anywhere else."""
    w: NoiseWaveform | PskWaveform = (
        NoiseWaveform(KEY, 25e3) if name == "noise" else PskWaveform(KEY, 25_000, 4, 0.2)
    )
    rate = w.grid_rate_hz
    fs = ratio * rate
    n = int(48 * BLK * ratio)
    base = T0_NS + 4_321
    t = np.arange(n) / fs
    y = w.evaluate(base, t)
    assert w.blocks_built >= 48
    bw = w.bandwidth_hz
    assert _leakage_db(y, fs, bw) < -108.0
    phase = _block_phase(base, t, rate)
    assert abs(_edge_excess_db(y, fs, bw, phase)) < 4.0

    # Negative controls on the same record. (1) a 0.3 % step at every block edge:
    block_index = np.floor((phase[0] + np.arange(n) * (rate / fs)) / BLK)
    broken = y + 3e-3 * (block_index % 2)
    assert _leakage_db(broken, fs, bw) > -97.0
    assert _edge_excess_db(broken, fs, bw, phase) > 20.0
    # (2) one grid sample dropped at every block edge (an off-by-one in the stitching):
    slipped = w.evaluate(base, t + block_index / rate)
    assert _leakage_db(slipped, fs, bw) > -60.0
    assert _edge_excess_db(slipped, fs, bw, phase) > 20.0


def test_block_boundaries_are_seamless_across_calls_and_cache_evictions():
    """The same 48 blocks fetched in 37 calls of uneven length, each with its own
    base_ns, through a 2-block cache: same samples as one call, same clean spectrum."""
    w = NoiseWaveform(KEY, 25e3, cache_blocks=2)
    fs = 62_500.0  # 16 000 ns per sample: bases below are exact
    n = 48 * BLK * 5 // 8
    base = T0_NS + 4_321
    cuts = np.unique(np.concatenate([[0, n], np.random.default_rng(13).integers(1, n, 36)]))
    pieces = [
        w.evaluate(base + int(a) * 16_000, np.arange(int(b - a)) / fs)
        for a, b in zip(cuts[:-1], cuts[1:])
    ]
    y = np.concatenate(pieces)
    assert len(w._cache) <= 2
    whole = NoiseWaveform(KEY, 25e3).evaluate(base, np.arange(n) / fs)
    assert np.max(np.abs(y - whole)) < 1e-9
    assert _leakage_db(y, fs, 25e3) < -108.0


# ------------------------------------------------------------------- performance


def test_evaluate_has_no_python_loop_over_samples():
    """Counts interpreter-level calls while rendering 2**18 samples: a loop over samples
    would make at least one call per sample; block and chunk loops make a few thousand.
    Independent of how busy the machine is."""
    n = 1 << 18
    calls = 0

    def count(frame, event, arg):  # noqa: ANN001
        nonlocal calls
        if event in ("call", "c_call"):
            calls += 1

    cases: list[tuple[Waveform, float]] = [
        (NoiseWaveform(KEY, 1e6), 1.25e6),
        (PskWaveform(KEY, 1_000_000, 4, 0.35), 1.6875e6),
        (ChirpWaveform(1e6, 1e-3, duty=0.5, period_s=7e-4), 1.25e6),
    ]
    previous = sys.getprofile()
    for w, fs in cases:
        t = np.arange(n) / fs
        calls = 0
        sys.setprofile(count)
        try:
            y = w.evaluate(T0_NS, t)
        finally:
            sys.setprofile(previous)
        assert y.shape == (n,)
        assert calls < n // 8, (type(w).__name__, calls)

    # the channel routines on the same number of samples
    noise = NoiseWaveform(KEY, 1e6)
    calls = 0
    sys.setprofile(count)
    try:
        y = render_path(noise, T0_NS, n, 2.5e6, lambda t: 0.25 + 1e-9 * t, lambda t: 100.0 * t, 1.0)
        z = decimate(y, 2)
        v = thermal_noise(n, "thermal|1|c|0|1.0|1.0")
    finally:
        sys.setprofile(previous)
    assert y.shape == (n,) and z.shape == (n // 2,) and v.shape == (n,)
    assert calls < n // 8, calls


# ------------------------------------------------------------- channel: decimate


@pytest.mark.parametrize("m", [2, 3, 4, 7, 16])
def test_decimation_filter_response(m):
    """MATH.md section 4: linear phase, passband to 0.4 fs, at least 60 dB beyond 0.5 fs
    (fs = output rate), from a 2**20-point transform of the taps."""
    h = decimation_fir(m)
    assert h.size % 2 == 1
    np.testing.assert_array_equal(h, h[::-1])  # symmetric: linear phase
    half = (h.size - 1) // 2
    assert half % m == 0  # group delay is a whole number of output samples
    assert abs(float(np.sum(h)) - 1.0) < 1e-12
    nfft = 1 << 20
    resp = np.abs(np.fft.rfft(h, nfft))
    f_out = np.arange(resp.size) * (m / nfft)  # in units of the output rate
    assert np.max(np.abs(resp[f_out <= 0.4] - 1.0)) < 2e-4
    assert 20 * math.log10(resp[f_out >= 0.5].max()) < -80.0  # 20 dB better than required


@pytest.mark.parametrize("m,n", [(2, 4001), (3, 6000), (4, 8003), (7, 9100)])
def test_decimate_is_the_filter_at_every_mth_input_instant(m, n):
    """Brute force: y[i] = sum_j h[j] x[i m + half - j], x zero outside the array, so
    output i sits on input sample i * m. Includes lengths that are not multiples of m."""
    rng = np.random.default_rng(14)
    x = rng.standard_normal(n) + 1j * rng.standard_normal(n)
    h = decimation_fir(m)
    half = (h.size - 1) // 2
    padded = np.concatenate([np.zeros(half, complex), x, np.zeros(half + m, complex)])
    n_out = -(-n // m)
    y = decimate(x, m)
    assert y.shape == (n_out,) and y.dtype == np.complex128
    for i in list(range(0, 40)) + list(range(n_out - 40, n_out)) + [n_out // 2, n_out // 3]:
        # padded[i m + half - j + half] for j = 0..2 half: the window centred on input i m
        window = padded[i * m : i * m + h.size][::-1]
        assert abs(y[i] - np.dot(h, window)) < 1e-12, i
    yr = decimate(x.real, m)
    assert yr.dtype == np.float64
    np.testing.assert_allclose(yr, y.real, rtol=0, atol=1e-13)


@pytest.mark.parametrize("m", [2, 4, 5])
def test_decimate_passes_the_band_with_zero_delay_and_rejects_aliases(m):
    """A multitone inside 0.4 fs comes out equal to its own samples at instants i * m
    (so the group delay is removed to a small fraction of an input sample); tones
    between 0.5 fs and the input Nyquist come out 80 dB down."""
    rng = np.random.default_rng(15)
    n_out = 3000
    k = np.arange(n_out * m)
    f_in = rng.uniform(-0.4, 0.4, 12)  # cycles per output sample
    amp = rng.uniform(0.5, 1.0, 12) * np.exp(2j * np.pi * rng.random(12))
    x = (amp[None, :] * np.exp(2j * np.pi * np.outer(k, f_in / m))).sum(axis=1)
    y = decimate(x, m)
    edge = decimate_margin(m)
    core = slice(edge, n_out - edge)
    assert _db(_rms(y[core] - x[::m][core]) / _rms(x)) < -75.0
    # a residual delay of one input sample would be seen at about -10 dB:
    assert _db(_rms(y[core] - x[1::m][core]) / _rms(x)) > -25.0
    # the edge transient is confined to the stated margin
    assert _db(float(np.max(np.abs(y[edge : edge + 5] - x[::m][edge : edge + 5]))) / _rms(x)) < -70.0
    for f_alias in np.concatenate([[0.5, 0.5 * m], rng.uniform(0.5, 0.5 * m, 10)]):
        for sign in (1.0, -1.0):
            tone = np.exp(2j * np.pi * sign * f_alias / m * k)
            assert _db(float(np.max(np.abs(decimate(tone, m)[core])))) < -80.0, (m, f_alias)


def test_decimated_oversampled_render_equals_direct_render():
    """The simulator's straddling path for a signal that is in fact inside the passband:
    render at m fs, decimate, compare with the render at fs (same first-sample time)."""
    w = NoiseWaveform(KEY, 18e3)  # +/- 9 kHz, moved to within +/- 12 kHz: inside 0.4 * 31.25 kHz
    fs, n = 31_250.0, 3000
    delay = lambda t: 0.2501234 + 2.0e-6 * t  # noqa: E731
    phase = lambda t: 0.3 + 3000.0 * t - 1.5 * t * t  # noqa: E731
    direct = render_path(w, T0_NS + 5, n, fs, delay, phase, 0.7)
    for m in (2, 3, 4):
        over = render_path(w, T0_NS + 5, n * m, fs * m, delay, phase, 0.7)
        got = decimate(over, m)
        core = slice(decimate_margin(m), n - decimate_margin(m))
        assert _db(_rms(got[core] - direct[core]) / _rms(direct)) < -75.0


# ---------------------------------------------------------- channel: render_path


def test_render_path_against_from_scratch_noise_and_closed_form_phase():
    """y[k] = a s(base + k/fs - D(k/fs)) exp(2j pi phi(k/fs)) with s rebuilt from the
    documents at the exact instants; D of bent-pipe size (0.25 s, drifting)."""
    w = NoiseWaveform(KEY, 25e3)
    fs, n = 31_250.0, 4096
    base = T0_NS + 123_456_789
    d0, d1, d2 = 0.2512345678, -3.1e-6, 2.0e-9
    f0, f1 = -1234.5, 2.5
    amp = 0.37 * np.exp(0.4j)
    y = render_path(
        w, base, n, fs,
        lambda t: d0 + d1 * t + d2 * t * t,
        lambda t: 1.0e8 + 0.125 + f0 * t + 0.5 * f1 * t * t,  # 1e8 whole cycles, as at Ku band
        amp,
    )
    assert y.dtype == np.complex128 and y.shape == (n,)
    k = np.array([0, 1, 2, 777, 2048, 4000, 4095])
    t = k / fs
    envelope = _noise_from_scratch(w, KEY, base, t - (d0 + d1 * t + d2 * t * t))
    # 1e8 + x is rounded to 1.5e-8 cycle in float64; the expected value uses the same sum
    cycles = np.array([float(Fraction(1.0e8 + 0.125 + f0 * ti + 0.5 * f1 * ti * ti) % 1) for ti in t])
    expected = amp * envelope * np.exp(2j * np.pi * cycles)
    assert np.max(np.abs(y[k] - expected)) < 3e-5 * abs(amp)


def test_render_path_sign_conventions():
    """Positive delay = the envelope arrives later; positive phase slope = positive
    baseband frequency; phase_fn is in cycles."""
    fs, n = 1.0e6, 6000  # 1 microsecond steps
    period_ns = 2_000_000
    pulse = CwWaveform(duty=0.25, period_s=period_ns * 1e-9)  # on for 0.5 ms from each 2 ms mark
    assert T0_NS % period_ns == 0
    delay_s = 0.2503215  # 125 whole periods + 321.5 us
    y = render_path(pulse, T0_NS, n, fs, lambda t: np.full_like(t, delay_s), None, 2.0)
    on = np.abs(y) > 0
    np.testing.assert_array_equal(np.abs(y[on]), 2.0)
    rises = np.flatnonzero(on[1:] & ~on[:-1]) + 1
    falls = np.flatnonzero(~on[1:] & on[:-1]) + 1
    np.testing.assert_array_equal(rises, [322, 2322, 4322])  # first sample at or after 321.5 us
    np.testing.assert_array_equal(falls, [822, 2822, 4822])
    # a negative "delay" (clock ahead) moves the pulse earlier by the same amount
    early = np.abs(render_path(pulse, T0_NS, n, fs, lambda t: np.full_like(t, -321.5e-6), None, 1.0)) > 0
    np.testing.assert_array_equal(np.flatnonzero(early[1:] & ~early[:-1]) + 1, [1679, 3679, 5679])

    # a shrinking delay (approaching path) compresses the envelope time axis: s is
    # evaluated at t (1 + r) - D0, so a chirp reads its sweep rate times (1 + r)^2
    chirp = ChirpWaveform(200e3, 4e-3)
    r = 1.0e-3
    z = render_path(chirp, T0_NS, 3000, fs, lambda t: 0.2 - r * t, None, 1.0)
    inst = np.angle(z[1:] * np.conj(z[:-1])) * fs / (2 * np.pi)
    slope = np.polyfit(np.arange(100, 2900) / fs, inst[100:2900], 1)[0]
    assert slope == pytest.approx(200e3 / 4e-3 * (1 + r) ** 2, rel=1e-6)
    assert slope != pytest.approx(200e3 / 4e-3, rel=1e-3)

    # phase: +f in cycles per second lands on the +f FFT bin; a quarter cycle is +j
    nfft = 4096
    tone = render_path(CwWaveform(), T0_NS, nfft, fs, None, lambda t: 0.25 + (37 * fs / nfft) * t, 1.5)
    spec = np.fft.fft(tone) / nfft
    assert int(np.argmax(np.abs(spec))) == 37
    assert spec[37] == pytest.approx(1.5j, abs=1e-9)


def test_two_rendered_paths_correlate_at_tdoa_and_fdoa():
    """MATH.md section 6: chi(tau, nu) = sum y_A(t + tau) conj(y_B(t)) exp(-2j pi nu t)
    peaks at tau = D_A - D_B and nu = f_A - f_B; here with a sub-sample TDOA."""
    w = NoiseWaveform(KEY, 25e3)
    fs, n = 50e3, 16_384
    tdoa = 13.25 / fs
    d_b = 0.2400
    d_a = d_b + tdoa
    f_a, f_b = 310.0, 847.5
    ya = render_path(w, T0_NS, n, fs, lambda t: np.full_like(t, d_a), lambda t: f_a * t, 1.0)
    yb = render_path(w, T0_NS, n, fs, lambda t: np.full_like(t, d_b), lambda t: f_b * t, 1.0)
    t = np.arange(n) / fs
    spec_b = np.fft.fft(yb * np.exp(2j * np.pi * (f_a - f_b) * t))  # Doppler-aligned B
    xc = np.fft.ifft(np.fft.fft(ya) * np.conj(spec_b))  # xc[l] = sum ya[m + l] conj(yb'[m])
    lags = np.arange(-40, 41)
    mag = np.abs(xc[lags])
    assert lags[int(np.argmax(mag))] == 13
    # parabolic interpolation of the peak: 13.25 samples
    i = int(np.argmax(mag))
    frac = 0.5 * (mag[i - 1] - mag[i + 1]) / (mag[i - 1] - 2 * mag[i] + mag[i + 1])
    assert lags[i] + frac == pytest.approx(13.25, abs=0.05)
    assert mag.max() / n > 0.9
    # with the wrong sign of nu the peak is gone
    wrong = np.fft.ifft(np.fft.fft(ya) * np.conj(np.fft.fft(yb * np.exp(-2j * np.pi * (f_a - f_b) * t))))
    assert np.abs(wrong[lags]).max() / n < 0.1
    # swapping the channels flips the delay
    swapped = np.abs(np.fft.ifft(np.conj(np.fft.fft(ya)) * spec_b)[lags])
    assert lags[int(np.argmax(swapped))] == -13


# -------------------------------------------------------- channel: thermal noise


def test_thermal_noise_is_the_keyed_unit_power_white_sequence():
    label = "thermal|1|hnl-a|1790000000000000000|31250.0|259000000.0"
    n = 131_072
    x = thermal_noise(n, label)
    key = int.from_bytes(hashlib.blake2b(label.encode(), digest_size=16).digest(), "little")
    z = np.random.Generator(np.random.Philox(key=key)).standard_normal(2 * n)
    np.testing.assert_array_equal(x, (z[0::2] + 1j * z[1::2]) / math.sqrt(2.0))
    assert x.dtype == np.complex128
    assert abs(np.mean(np.abs(x) ** 2) - 1.0) < 0.012  # E|x|^2 = 1, sigma 0.28 %
    assert abs(np.mean(x * x)) < 0.012 and abs(np.mean(x)) < 0.012
    assert np.abs(_autocorr(x))[1:].max() < 0.02  # white at every lag (sigma 0.0028)
    np.testing.assert_array_equal(thermal_noise(1000, label), x[:1000])
    other = thermal_noise(n, label.replace("hnl-a", "hnl-b"))
    assert abs(np.vdot(x, other)) / n < 0.015
    # 0 dB per bin in the PSD convention of MATH.md section 7
    f, p = _spectrum(x, 1.0, 256, beta=6.0)
    assert abs(np.mean(p) - 1.0) < 0.02 and p.max() / p.min() < 1.6


# --------------------------------------------- copies, pickles and invalid input


@pytest.mark.parametrize("name", ["cw", "chirp", "noise", "qpsk"])
def test_waveforms_can_be_copied_and_pickled(name):
    """A waveform is a pure function of time and a key, so a copy (copy, deepcopy,
    pickle: what dataclasses.asdict or a process pool would do) must give the same
    values; the block cache and its lock are not part of the definition."""
    w = _zoo()[name]
    t = np.arange(300) / 31_250.0
    ref = w.evaluate(T0_NS + 3, t)  # fills the cache of the original
    for clone in (copy.copy(w), copy.deepcopy(w), pickle.loads(pickle.dumps(w))):
        assert type(clone) is type(w)
        assert clone.bandwidth_hz == w.bandwidth_hz and clone.rms_bandwidth_hz == w.rms_bandwidth_hz
        np.testing.assert_array_equal(clone.evaluate(T0_NS + 3, t), ref)
        if isinstance(w, (NoiseWaveform, PskWaveform)):
            assert clone._cache is not w._cache and clone._lock is not w._lock
            assert not clone.fir.flags.writeable
            assert 1 <= clone.blocks_built <= 3  # it built its own blocks
    if isinstance(w, NoiseWaveform):
        assert len(pickle.dumps(w)) < 200_000  # the 32 MiB cache does not travel


def test_invalid_input_raises_value_error_naming_the_argument():
    w = NoiseWaveform(KEY, 25e3)
    psk = PskWaveform(KEY, 1000)
    with pytest.raises(ValueError, match="t_rel"):  # numpy alone would drop the imaginary part
        w.evaluate(T0_NS, np.array([0.0, 1e-3 + 5j]))
    with pytest.raises(ValueError, match="t_rel"):
        CwWaveform().evaluate(T0_NS, np.array([1j]))
    with pytest.raises(ValueError, match="t_rel"):
        w.evaluate(T0_NS, ["a"])
    with pytest.raises(ValueError, match="t_rel"):
        w.evaluate(T0_NS, None)
    with pytest.raises(ValueError, match="t_rel"):
        w.gate(T0_NS, np.array([1j]))
    with pytest.raises(ValueError, match="freqs_hz"):
        w.spectral_shape(np.array([0.0, 1j]))
    with pytest.raises(ValueError, match="k_first"):
        w.grid_samples(1.5, 3)
    with pytest.raises(ValueError, match="n_first"):
        psk.symbols(1.5, 3)
    with pytest.raises(ValueError, match="block"):
        raw_block(KEY, 1.7)  # used to be truncated to block 1 without a word
    with pytest.raises(ValueError, match="t_sym"):
        rrc_pulse("a", 0.3)
    with pytest.raises(ValueError, match="rolloff"):
        rrc_pulse(0.0, None)
    for bad_fs in (None, "abc", np.array([1e3, 2e3]), True, 0.0, -1.0, float("inf")):
        with pytest.raises(ValueError, match="fs"):
            render_path(w, T0_NS, 4, bad_fs, None, None, 1.0)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="waveform"):
        render_path(None, T0_NS, 4, 1e3, None, None, 1.0)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="delay_fn"):
        render_path(w, T0_NS, 4, 1e3, 0.25, None, 1.0)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="phase_fn"):
        render_path(w, T0_NS, 4, 1e3, None, 0.5, 1.0)  # type: ignore[arg-type]
    # the accepted forms still work: lists, ints, float32, numpy scalars for fs
    assert w.evaluate(T0_NS, [0, 1]).shape == (2,)
    assert w.evaluate(T0_NS, np.zeros(3, dtype=np.float32)).shape == (3,)
    assert render_path(w, T0_NS, 4, np.float64(1e3), None, None, 1).shape == (4,)
    assert render_path(w, T0_NS, 4, 1000, None, None, 1).shape == (4,)


def test_render_path_does_not_relabel_errors_raised_inside_the_callbacks():
    """A ValueError or TypeError from the caller's delay or phase function (an orbit
    table out of span, for example) must arrive as raised, not rewritten as a complaint
    about the function's return value."""

    class OutOfSpan(ValueError):
        pass

    def delay(t: np.ndarray) -> np.ndarray:
        raise OutOfSpan("t_s outside the table span")

    def phase(t: np.ndarray) -> np.ndarray:
        raise TypeError("unsupported operand in the phase model")

    with pytest.raises(OutOfSpan) as info:
        render_path(CwWaveform(), T0_NS, 4, 1e3, delay, None, 1.0)
    assert str(info.value) == "t_s outside the table span"
    assert info.traceback[-1].name == "delay"  # the traceback still reaches the culprit
    with pytest.raises(TypeError) as info2:
        render_path(CwWaveform(), T0_NS, 4, 1e3, None, phase, 1.0)
    assert str(info2.value) == "unsupported operand in the phase model"
    # what the function returns is still checked, by name
    with pytest.raises(ValueError, match="delay_fn"):
        render_path(CwWaveform(), T0_NS, 4, 1e3, lambda t: np.zeros(3), None, 1.0)
    with pytest.raises(ValueError, match="delay_fn"):
        render_path(CwWaveform(), T0_NS, 4, 1e3, lambda t: np.full_like(t, np.nan), None, 1.0)

    # the time axis is shared by both callbacks and the envelope: changing it in place
    # raises instead of silently moving the phase model by a second
    def in_place(t: np.ndarray) -> np.ndarray:
        t += 1.0
        return np.zeros_like(t)

    with pytest.raises(ValueError, match="read-only"):
        render_path(CwWaveform(), T0_NS, 4, 1e3, in_place, lambda t: 0.25 * t, 1.0)
    # returning the axis itself (or a view of it) is fine
    y = render_path(CwWaveform(), T0_NS, 4, 4.0, None, lambda t: t, 1.0)  # 0, 1/4, 1/2, 3/4 cycle
    np.testing.assert_allclose(y, [1, 1j, -1, -1j], atol=1e-15)
    with pytest.raises(ValueError, match="phase_fn"):
        render_path(CwWaveform(), T0_NS, 4, 1e3, None, lambda t: t * 1j, 1.0)
