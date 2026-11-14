"""Independent verification of core/caf.py and core/crlb.py.

Nothing here reuses the generators of test_caf.py or the estimator's internals for the
expected values. Channel A comes from processes that can be evaluated exactly at any
time (band-limited periodic Gaussian processes, an analytic sawtooth chirp, a chirp-z
evaluation for drifting delays), so the CAF can be summed straight from its definition
(MATH.md section 6) at fractional delays without interpolating samples. Expected
sigmas come from closed forms (MATH.md section 8) or from an explicit 2x2 covariance,
written out here, not from crlb.
"""
from __future__ import annotations

import math

import numpy as np
import pytest
from scipy import fft as sfft
from scipy.signal import czt

from petal_locate.core import caf as caf_mod
from petal_locate.core import crlb
from petal_locate.core.caf import caf_estimate

FS = 31250.0


# ------------------------------------------------------------------------- generators


class Process:
    """Periodic band-limited complex Gaussian process of unit power, exact at any time t (s).

    s(t) = (1 / P) sum_k S_k exp(j 2 pi f_k t) over the occupied Fourier bins of a period
    of P samples at fs; ``at(t_first, n)`` returns s(t_first + m / fs), m = 0..n-1.
    """

    def __init__(self, seed: int, n_period: int, fs: float, bw: float) -> None:
        rng = np.random.default_rng(seed)
        self.fs, self.n_period = fs, n_period
        self.f = sfft.fftfreq(n_period, 1.0 / fs)
        band = np.abs(self.f) <= bw / 2.0
        spec = np.zeros(n_period, dtype=complex)
        k = int(np.count_nonzero(band))
        spec[band] = rng.standard_normal(k) + 1j * rng.standard_normal(k)
        self.spec = spec * (n_period / math.sqrt(float(np.sum(np.abs(spec) ** 2))))

    def at(self, t_first: float, n: int) -> np.ndarray:
        return sfft.ifft(self.spec * np.exp(2j * np.pi * self.f * t_first))[:n]

    def stretched(self, x0: float, step: float, n: int) -> np.ndarray:
        """s(x0 + m * step), m = 0..n-1, any step (chirp-z transform over the occupied bins)."""
        k = np.flatnonzero(self.spec)
        k = k[np.argsort(self.f[k])]
        f_k, df = self.f[k], self.fs / self.n_period
        coef = self.spec[k] * np.exp(2j * np.pi * f_k * x0) / self.n_period
        out = czt(coef, m=n, w=np.exp(2j * np.pi * df * step), a=1.0)
        return out * np.exp(2j * np.pi * f_k[0] * step * np.arange(n))

    def direct(self, x: float) -> complex:
        return complex(np.sum(self.spec * np.exp(2j * np.pi * self.f * x)) / self.n_period)


def cnoise(rng: np.random.Generator, n: int, power: float = 1.0) -> np.ndarray:
    return math.sqrt(power / 2.0) * (rng.standard_normal(n) + 1j * rng.standard_normal(n))


def sawtooth(t: np.ndarray, bw: float, period: float) -> np.ndarray:
    """Linear FM sweeping -bw/2 .. +bw/2 (bw < 0: downwards) every `period` seconds."""
    u = np.mod(t, period)
    return np.exp(2j * np.pi * (-0.5 * bw * u + 0.5 * (bw / period) * u * u))


def sawtooth_moments(t_first: float, duration: float, bw: float, period: float) -> tuple[float, float]:
    """(time-frequency correlation, RMS bandwidth in Hz) of the sweep over [t_first, t_first + duration).

    Instantaneous frequency against time by quadrature on a fine grid (constant envelope).
    """
    tt = t_first + (np.arange(400_000) + 0.5) / 400_000 * duration
    f_inst = bw / period * np.mod(tt, period) - 0.5 * bw
    return float(np.corrcoef(tt, f_inst)[0, 1]), float(np.std(f_inst))


class Pair:
    """Channel A (exact at any array position) and channel B samples of one emitter.

    Array convention of MATH.md section 6: ya[m] at labelled time tA + m / fs, yb[n] at
    tB + n / fs, t0 = tA - tB; times below are relative to tB. Channel B hears the
    emitter with delay D_B and baseband offset F; channel A with D_B + tdoa and
    F + fdoa. A's noise is a band-limited process too, so A is exact between samples.
    """

    D_B, F = 1.1e-3, 4.3

    def __init__(
        self, seed: int, na: int, nb: int, tdoa: float, fdoa: float, t0: float, snr_a_db: float, snr_b_db: float,
        *, chirp: tuple[float, float] | None = None,
    ) -> None:
        self.na, self.nb, self.t0, self.tdoa, self.fdoa = na, nb, t0, tdoa, fdoa
        n_period = int(sfft.next_fast_len(2 * max(na, nb) + 4000))
        self.chirp = chirp
        self.src = Process(seed, n_period, FS, 25e3)
        self.noise_a = Process(seed + 1, n_period, FS, 0.95 * FS)
        self.amp_a = 10.0 ** (-snr_a_db / 20.0)
        rng = np.random.default_rng(seed + 2)
        t_b = np.arange(nb) / FS
        self.yb = self._signal(t_b - self.D_B) * np.exp(2j * np.pi * self.F * t_b) + cnoise(rng, nb, 10.0 ** (-snr_b_db / 10.0))
        self.ya = self.a_at(0.0, 0, na)

    def _signal(self, x: np.ndarray) -> np.ndarray:
        if self.chirp is not None:
            return sawtooth(x, *self.chirp)
        return self.src.at(float(x[0]), x.size)  # x is always a uniform grid with step 1 / fs here

    def a_at(self, lam: float, first: int, count: int) -> np.ndarray:
        """y_A at array positions first + m + lam (samples), m = 0..count-1."""
        pos = first + lam + np.arange(count)
        t = self.t0 + pos / FS  # labelled time relative to tB
        sig = self._signal(t - self.D_B - self.tdoa) * np.exp(2j * np.pi * (self.F + self.fdoa) * t)
        return sig + self.amp_a * self.noise_a.at((first + lam) / FS, count)


def exact_chi(p: Pair, n_sum: int, lags: np.ndarray, nus: np.ndarray) -> np.ndarray:
    """chi[i, j] = sum_n y_A(t_n + lag_i) conj(y_B(t_n)) exp(-j 2 pi nu_j t_n) (MATH.md section 6).

    Summed over the n_sum samples of B centred on B's midpoint (the samples caf_estimate
    reports using: integration_s * fs of them), t_n from the midpoint of all B samples.
    """
    start = (p.nb - n_sum) // 2
    n = np.arange(start, start + n_sum)
    t = (n - 0.5 * (p.nb - 1)) / FS
    phase = np.exp(-2j * np.pi * np.outer(nus, t))
    out = np.empty((lags.size, nus.size), dtype=complex)
    for i, lag in enumerate(lags):
        out[i] = phase @ (p.a_at(float(lag), start, n_sum) * np.conj(p.yb[n]))
    return out


def exact_peak(p: Pair, n_sum: int, lag0: float, nu0: float) -> tuple[float, float, float]:
    """Maximum of |chi|^2 near (lag0 samples, nu0 Hz) by repeated zooming of an 11 x 11 grid."""
    span_l, span_n = 1.5, 1.5 * FS / n_sum
    p_max = 0.0
    for _ in range(6):
        lags = lag0 + np.linspace(-span_l, span_l, 11)
        nus = nu0 + np.linspace(-span_n, span_n, 11)
        power = np.abs(exact_chi(p, n_sum, lags, nus)) ** 2
        i, j = np.unravel_index(int(np.argmax(power)), power.shape)
        lag0, nu0, p_max = float(lags[i]), float(nus[j]), float(power[i, j])
        span_l *= 0.3
        span_n *= 0.3
    return lag0, nu0, p_max


# ------------------------------------------------------------ brute force against the CAF

BRUTE_CASES = [
    # seed, na, nb, tdoa (s), fdoa (Hz), t0_offset (s), delay window, doppler window, chirp
    (1, 2048, 2048, -0.731e-3, 17.3, 0.0, (-1.0e-3, 0.5e-3), (-40.0, 40.0), None),  # negative delay
    (2, 2048, 2048, 0.99e-3, -5.1, 0.0, (0.2e-3, 0.99e-3 + 0.3 / FS), (-40.0, 40.0), None),  # 0.3 sample inside the top edge
    (3, 2048, 2048, 1.105e-3, 3.3, 0.38e-3, (1.105e-3 - 0.2 / FS, 2.0e-3), (-40.0, 40.0), None),  # bottom edge, A late
    (4, 2048, 2048, 0.317e-3, 38.9, 0.0, (-0.5e-3, 1.0e-3), (-40.0, 39.5), None),  # Doppler at the window edge
    (5, 2400, 2048, -5.123e-3, -21.7, -0.52e-3, (-6e-3, -4e-3), (-30.0, 10.0), None),  # A longer, B late
    (6, 2048, 2600, 3.777e-3, 0.37, 1.234567e-3, (3e-3, 4.5e-3), (-10.0, 10.0), None),  # A shorter, A late
    (7, 4096, 4096, 12.37 / FS, 3.0, 0.0, (12.37 / FS - 20e-6, 12.37 / FS + 20e-6), (-3.0, 9.0), (20e3, 4096 / FS / 2)),
]


@pytest.mark.parametrize("case", BRUTE_CASES, ids=[f"case{c[0]}" for c in BRUTE_CASES])
def test_peak_is_the_maximum_of_the_exact_caf(case: tuple) -> None:
    """The estimate is the maximum of |chi|^2 summed from the definition, over the whole window."""
    seed, na, nb, tdoa, fdoa, t0, dwin, nwin, chirp = case
    p = Pair(seed, na, nb, tdoa, fdoa, t0, -3.0, -3.0, chirp=chirp)
    r = caf_estimate(p.ya, p.yb, FS, delay_window=dwin, doppler_window=nwin, t0_offset_s=t0)
    assert r.reason is None and r.tdoa_s is not None and r.fdoa_hz is not None
    assert r.sigma_tdoa_s is not None and r.sigma_fdoa_hz is not None
    n_sum = int(round(r.integration_s * FS))
    lag_est = (r.tdoa_s - t0) * FS
    lag_bf, nu_bf, p_bf = exact_peak(p, n_sum, lag_est, r.fdoa_hz)
    # Same function, same samples: the estimate sits on the exact maximum. The fine search
    # delays A by an FFT phase ramp over its own samples, which puts a small random
    # error at the edges (measured over 40 random cases: std 0.04 sigma, at most 0.10).
    assert abs(lag_est - lag_bf) / FS < 0.15 * r.sigma_tdoa_s
    assert abs(r.fdoa_hz - nu_bf) < 0.15 * r.sigma_fdoa_hz
    # ... and it is the global maximum: no point of an integer-lag grid across the
    # window (one lag beyond each edge) by a Doppler grid of a quarter bin beats it.
    t_sum = n_sum / FS
    lags = np.arange(math.floor((dwin[0] - t0) * FS) - 1, math.ceil((dwin[1] - t0) * FS) + 2).astype(float)
    nus = np.arange(nwin[0], nwin[1] + 1e-9, 0.25 / t_sum)
    grid = np.abs(exact_chi(p, n_sum, lags, nus)) ** 2
    assert float(grid.max()) <= p_bf * (1.0 + 1e-6)
    # The truth is within the noise.
    assert abs(r.tdoa_s - tdoa) < 4.5 * r.sigma_tdoa_s and abs(r.fdoa_hz - fdoa) < 4.5 * r.sigma_fdoa_hz


def test_exact_process_generators_agree_with_direct_summation() -> None:
    """The generators used above against their Fourier series summed term by term."""
    proc = Process(5, 4096, FS, 25e3)
    x0, step = -1.23456e-3, (1.0 - 2e-5) / FS
    grid = proc.at(x0, 64)
    stretched = proc.stretched(x0, step, 64)
    for m in (0, 1, 33, 63):
        assert abs(grid[m] - proc.direct(x0 + m / FS)) < 1e-9
        assert abs(stretched[m] - proc.direct(x0 + m * step)) < 1e-9


# --------------------------------------------------------- sample and bin bias, no noise


def _sigma_30db(r: caf_mod.CafResult, rms_hz: float) -> tuple[float, float]:
    """Stein sigmas at 30 dB post-correlation SNR for the estimator's own B and T (MATH.md section 8)."""
    b, t = r.noise_bandwidth_hz, r.integration_s
    c = 1000.0 / (b * t)
    base = math.sqrt((1.0 - c) / (2.0 * b * t * c))
    return base / (2.0 * math.pi * rms_hz), base / (math.pi * t / math.sqrt(3.0))


BIAS_CASES = [
    # na, nb, tdoa (s), fdoa (Hz), t0 (s), delay window (s), doppler window (Hz), signal_bw_hz
    (8192, 8192, -40.37 / FS, -11.13, 0.0, (-2.5e-3, -0.5e-3), (-40.0, 40.0), None),
    (8192, 8192, -40.5 / FS, 11.0 + 0.5 * 0.25 * FS / 8192, 0.0, (-2.5e-3, -0.5e-3), (-40.0, 40.0), 25e3),
    (8192, 8192, 33.25 / FS, 39.0, -0.52e-3 + 0.37 / FS, (0.5e-3, 1.5e-3), (-40.0, 39.4), None),
    (8192, 8192, 0.71 / FS, -39.2, 0.38e-3 + 0.11 / FS, (-0.2e-3, 0.3e-3), (-39.3, 40.0), 25e3),
    (9000, 8192, -123.9 / FS, 2.77, 1.0e-3, (-5e-3, -3e-3), (-10.0, 10.0), None),
    (8192, 9000, 222.01 / FS, -2.77, -1.0e-3, (6e-3, 8e-3), (-10.0, 10.0), 25e3),
    (8192, 8192, 0.0, 0.0, 0.0, (-0.5e-3, 0.5e-3), (-10.0, 10.0), None),
]


def test_noise_free_estimates_have_no_sample_or_bin_bias() -> None:
    """Noise-free copies off both grids: negative lags, sub-sample t0_offset_s, unequal lengths, window edges.

    MATH.md section 6 item 2 asks for a bias under 5 % of the CRLB at 30 dB. An
    off-by-one sample, a half-sample or half-bin convention slip, or a wrong sign of
    t0_offset_s would show up here as an error of thousands of percent.
    """
    rms = 25e3 / math.sqrt(12.0)
    err_t, err_f = [], []
    for i, (na, nb, tdoa, fdoa, t0, dwin, nwin, sbw) in enumerate(BIAS_CASES):
        p = Pair(400 + i, na, nb, tdoa, fdoa, t0, 200.0, 200.0)  # 200 dB: no noise to speak of
        r = caf_estimate(p.ya, p.yb, FS, delay_window=dwin, doppler_window=nwin, t0_offset_s=t0, signal_bw_hz=sbw)
        assert r.reason is None and r.tdoa_s is not None and r.coherence > 0.995, (i, r.reason)
        s_t, s_f = _sigma_30db(r, rms)
        err_t.append((r.tdoa_s - tdoa) / s_t)
        err_f.append((r.fdoa_hz - fdoa) / s_f)
    assert max(abs(e) for e in err_t) < 0.05, err_t
    assert max(abs(e) for e in err_f) < 0.05, err_f


# ----------------------------------------------------- post-correlation SNR at large lags


@pytest.mark.parametrize("sign", [1.0, -1.0])
def test_snr_db_is_b_t_c_at_large_lags(sign: float) -> None:
    """At |lag| = 0.35 N only 0.3 N samples overlap: snr_db must still be B T C for them.

    Peak power grows as the square of the samples summed and the noise floor as their
    number, so a floor fitted over rows of different overlap misreads the SNR (the
    original surface read 24.4 dB here instead of 27.9 dB, so a 16 dB copy at this
    lag would have been missed).
    """
    n = 8192
    tdoa = sign * (0.35 * n + 0.3) / FS
    snr, btc, coh = [], [], []
    for seed in range(4):
        rng = np.random.default_rng(seed)
        src = Process(100 + seed, int(sfft.next_fast_len(3 * n)), FS, 0.95 * FS)
        t = np.arange(n) / FS
        ya = src.at(-tdoa, n) * np.exp(2j * np.pi * 7.0 * t) + cnoise(rng, n)
        yb = src.at(0.0, n) + cnoise(rng, n)
        r = caf_estimate(ya, yb, FS, delay_window=(tdoa - 2e-3, tdoa + 2e-3), doppler_window=(-20.0, 20.0))
        assert r.reason is None and abs(r.tdoa_s - tdoa) < 4.0 * r.sigma_tdoa_s
        c = 0.25  # unit-power signal and unit-power noise in each channel: C = (1/2)^2
        snr.append(r.snr_db)
        btc.append(10.0 * math.log10(r.integration_s * FS * c))  # B = fs, T = samples summed / fs
        coh.append(r.coherence)
        assert abs(r.integration_s * FS - (n - 2 * abs(tdoa) * FS)) < 0.01 * n  # B samples symmetric about the midpoint
    assert float(np.mean(snr)) == pytest.approx(float(np.mean(btc)), abs=0.5)
    assert float(np.mean(coh)) == pytest.approx(0.25, rel=0.05)


# ------------------------------------------------------------------ reference epochs


@pytest.mark.parametrize(
    "na, nb, t0",
    [(16384, 12000, -2.0e-3), (12000, 16384, 3.5e-3)],
    ids=["A longer and earlier", "A shorter and later"],
)
def test_reference_epoch_is_the_midpoint_of_channel_b(na: int, nb: int, t0: float) -> None:
    """TDOA = D_A(t_mid + TDOA) - D_B(t_mid), FDOA = f_A(t_mid + TDOA) - f_B(t_mid), t_mid = middle of B.

    Both delays and both Dopplers drift linearly (D(t) = d0 + r t, f(t) = f0 + alpha t,
    t labelled time from the first B sample), within the limits of MATH.md section 6.
    The two captures have different lengths and start times, so the midpoints of A, of
    B and of their overlap are tens of milliseconds apart and any other epoch misses by
    about a microsecond and a tenth of a hertz.
    """
    d_a0, d_b0, r_a, r_b = 1.1e-3 + 17.3e-3, 1.1e-3, 1.2e-5, -0.8e-5
    f_a0, f_b0, al_a, al_b = -59.3, 3.7, 0.9, -0.9
    t_mid = (nb - 1) / (2.0 * FS)
    tdoa = (d_a0 + r_a * t_mid - d_b0 - r_b * t_mid) / (1.0 - r_a)  # solves tdoa = D_A(t_mid + tdoa) - D_B(t_mid)
    fdoa = f_a0 + al_a * (t_mid + tdoa) - (f_b0 + al_b * t_mid)
    t_a = t0 + np.arange(na) / FS
    t_b = np.arange(nb) / FS
    t_mid_a = t0 + (na - 1) / (2.0 * FS)
    wrong_epoch_t = abs(r_a - r_b) * abs(t_mid_a - t_mid)  # what A's midpoint as epoch would cost
    assert wrong_epoch_t > 0.4e-6
    err_t, err_f = [], []
    for seed in range(3):
        src = Process(700 + seed, int(sfft.next_fast_len(na + nb + 4000)), FS, 25e3)
        # s((1 - r) t - d0): a stretched grid of the periodic source
        ya = src.stretched((1.0 - r_a) * t_a[0] - d_a0, (1.0 - r_a) / FS, na) * np.exp(2j * np.pi * (f_a0 * t_a + 0.5 * al_a * t_a**2))
        yb = src.stretched(-d_b0, (1.0 - r_b) / FS, nb) * np.exp(2j * np.pi * (f_b0 * t_b + 0.5 * al_b * t_b**2))
        r = caf_estimate(
            ya, yb, FS, delay_window=(tdoa - 4e-3, tdoa + 4e-3), doppler_window=(-90.0, -40.0),
            signal_bw_hz=25e3, t0_offset_s=t0,
        )
        assert r.reason is None
        err_t.append(r.tdoa_s - tdoa)
        err_f.append(r.fdoa_hz - fdoa)
    wrong_epoch_f = abs(al_a - al_b) * abs(t_mid_a - t_mid)
    # The random signal weights the drift a little unevenly over the capture (tens of ns).
    assert abs(float(np.mean(err_t))) < 0.1 * wrong_epoch_t and max(abs(e) for e in err_t) < 0.2 * wrong_epoch_t
    assert abs(float(np.mean(err_f))) < 0.1 * wrong_epoch_f and max(abs(e) for e in err_f) < 0.2 * wrong_epoch_f


# ---------------------------------------- t0_offset_s, exclude and mirror_window together


def _copies(seed: int, n: int, t0: float, paths: list[tuple[float, float, float]], snr_b_db: float = 0.0):
    """Channel A hears the emitter on several paths (amplitude, tdoa, fdoa against B); A starts t0 after B."""
    src = Process(seed, int(sfft.next_fast_len(n + 5000)), FS, 25e3)
    rng = np.random.default_rng(seed + 1)
    t_a = t0 + np.arange(n) / FS
    t_b = np.arange(n) / FS
    ya = cnoise(rng, n)
    for amp, tdoa, fdoa in paths:
        ya = ya + amp * src.at(t0 - 1e-3 - tdoa, n) * np.exp(2j * np.pi * fdoa * t_a)
    yb = src.at(-1e-3, n) + cnoise(rng, n, 10.0 ** (-snr_b_db / 10.0))
    return ya, yb


@pytest.mark.parametrize("t0", [0.0, 7.3e-3 + 0.41 / FS, -6.1e-3])
def test_t0_offset_converts_windows_exclusions_mirror_and_outputs_alike(t0: float) -> None:
    """Every delay in and out is a true A-minus-B delay (MATH.md section 6), whatever t0_offset_s is.

    A strong self-peak (excluded), the emitter, and a co-channel copy 4 dB down in the
    mirror window. With t0_offset_s the result is the same as for the same samples
    described in array lags (no offset, every window and exclusion moved by -t0).
    """
    n = 31250
    true, self_pk, mirror = (17.3e-3, -63.0), (4.1e-3, 0.8), (9.0e-3, 31.0)
    ya, yb = _copies(31, n, t0, [(0.3, *true), (0.3 * 10.0 ** (8.0 / 20.0), *self_pk), (0.3 * 10.0 ** (-4.0 / 20.0), *mirror)])
    kw = dict(doppler_window=(-200.0, 200.0), signal_bw_hz=25e3)
    r = caf_estimate(
        ya, yb, FS, delay_window=(12e-3, 22e-3), exclude=[self_pk], mirror_window=((2e-3, 11e-3), (-100.0, 100.0)),
        t0_offset_s=t0, **kw,
    )
    assert r.reason is None and r.mirror_ambiguous
    assert abs(r.tdoa_s - true[0]) < 5.0 * r.sigma_tdoa_s and abs(r.fdoa_hz - true[1]) < 5.0 * r.sigma_fdoa_hz
    assert any(abs(c[0] - mirror[0]) < 3e-6 and abs(c[1] - mirror[1]) < 0.1 for c in r.candidates)
    assert all(abs(c[0] - self_pk[0]) > 0.2e-3 for c in r.candidates)  # the self-peak is notched
    assert r.delay_axis_s.min() <= r.tdoa_s <= r.delay_axis_s.max()
    assert r.delay_cut[0][0] == pytest.approx(12e-3, abs=1.0 / FS)
    # The same samples in array-lag terms.
    lag = caf_estimate(
        ya, yb, FS, delay_window=(12e-3 - t0, 22e-3 - t0), exclude=[(self_pk[0] - t0, self_pk[1])],
        mirror_window=((2e-3 - t0, 11e-3 - t0), (-100.0, 100.0)), **kw,
    )
    assert lag.reason is None and lag.mirror_ambiguous
    assert lag.tdoa_s == pytest.approx(r.tdoa_s - t0, abs=1e-12)
    assert lag.fdoa_hz == pytest.approx(r.fdoa_hz, abs=1e-9) and lag.snr_db == pytest.approx(r.snr_db, abs=1e-9)
    assert [c[0] for c in lag.candidates] == pytest.approx([c[0] - t0 for c in r.candidates], abs=1e-12)
    # Without the exclusion the self-peak (8 dB stronger) wins: the exclusion was applied
    # where t0_offset_s puts it, not at the raw array lag.
    blind = caf_estimate(ya, yb, FS, delay_window=(2e-3, 22e-3), t0_offset_s=t0, **kw)
    assert blind.tdoa_s == pytest.approx(self_pk[0], abs=2e-6)


# ------------------------------------------------------------------------ rho_tf


@pytest.mark.parametrize(
    "sweeps, sign, phase",
    [(2.5, 1.0, 0.0), (2.5, -1.0, 0.3), (1.3, 1.0, 0.55), (3.0, 1.0, 0.0), (1.7, -1.0, 0.8)],
)
def test_rho_tf_is_the_time_frequency_correlation_of_the_sweep(sweeps: float, sign: float, phase: float) -> None:
    """Partial sweeps and phases: rho_tf against the correlation of instantaneous frequency and time.

    The closed form is taken over exactly the samples summed (integration_s * fs of
    them, centred on the middle of B). High SNR, so the curvature is the waveform's.
    """
    n = 8192
    t = np.arange(n) / FS
    period = n / FS / sweeps
    bw = sign * 20e3
    tdoa, fdoa = 12.37 / FS, 3.0
    rng = np.random.default_rng(int(sweeps * 100 + phase * 10))
    ya = sawtooth(t - tdoa + phase * period, bw, period) * np.exp(2j * np.pi * fdoa * t) + cnoise(rng, n, 0.01)
    yb = sawtooth(t + phase * period, bw, period) + cnoise(rng, n, 0.01)
    r = caf_estimate(
        ya, yb, FS, delay_window=(tdoa - 20e-6, tdoa + 20e-6), doppler_window=(fdoa - 0.4 / period, fdoa + 0.4 / period)
    )
    n_sum = int(round(r.integration_s * FS))
    rho, rms = sawtooth_moments(phase * period + (n - n_sum) // 2 / FS, n_sum / FS, bw, period)
    assert abs(rho) < 0.85
    assert r.reason is None
    assert r.rho_tf == pytest.approx(rho, abs=0.02)
    assert r.rms_bandwidth_hz == pytest.approx(rms, rel=0.03)


# ------------------------------------------------------------------ invalid arguments


def test_invalid_scalar_arguments_raise_value_error_naming_them() -> None:
    rng = np.random.default_rng(1)
    ya, yb = cnoise(rng, 1024), cnoise(rng, 1024)
    ok = dict(delay_window=(-1e-3, 1e-3), doppler_window=(-50.0, 50.0))
    for name, value in [
        ("fs", None), ("fs", "fast"), ("t0_offset_s", None), ("t0_offset_s", "0"), ("signal_bw_hz", "wide"),
        ("signal_bw_hz", [1.0, 2.0]), ("min_snr_db", None), ("max_tdoa_sigma_s", None), ("max_tdoa_sigma_s", "x"),
    ]:
        args: dict = {**ok}
        fs: object = FS
        if name == "fs":
            fs = value
        else:
            args[name] = value
        with pytest.raises(ValueError, match=name):
            caf_estimate(ya, yb, fs, **args)  # type: ignore[arg-type]
    for call, name in [
        (lambda: crlb.stein_sigmas(1e3, 100.0, 1.0, "x", 1.0), "g1"),
        (lambda: crlb.stein_sigmas(None, 100.0, 1.0, 1.0, 1.0), "noise_bw_hz"),
        (lambda: crlb.stein_sigmas(1e3, 100.0, 1.0, 1.0, 1.0, rho_tf=1.0), "rho_tf"),
        (lambda: crlb.sigmas_from_coherence("x", 1e3, 100.0, 1.0), "C"),
        (lambda: crlb.sigmas_from_coherence(0.5, 1e3, 100.0, 1.0, rho_tf=0.5, signal_noise_share=1.5), "signal_noise_share"),
        (lambda: crlb.sigmas_from_coherence(0.5, 1e3, 100.0, 1.0, rho_tf=0.5, signal_noise_share="x"), "signal_noise_share"),
    ]:
        with pytest.raises(ValueError, match=name):
            call()


# ------------------------------------------------- coupled Stein bounds (crlb), closed form


def _explicit_covariance(b: float, rms: float, t: float, g1: float, g2: float, rho: float) -> np.ndarray:
    """Error covariance of the CAF peak, linearised, written out as 2x2 matrices.

    Peak curvature M ~ [[beta^2, -rho beta T_e], [-rho beta T_e, T_e^2]]; noise gradient:
    signal times noise with covariance a M (a = 1/g1 + 1/g2), noise times noise
    b diag(q beta^2, T_e^2) (b = 1/(g1 g2), flat noise over B: q = B^2 / (12 B_rms^2)).
    Covariance M^-1 (a M + b D) M^-1 / (2 B T), in (s, Hz).
    """
    beta, t_e = 2.0 * math.pi * rms, math.pi * t / math.sqrt(3.0)
    m = np.array([[beta**2, -rho * beta * t_e], [-rho * beta * t_e, t_e**2]])
    q = max(1.0, b * b / (12.0 * rms * rms))
    d = np.diag([q * beta**2, t_e**2])
    m_inv = np.linalg.inv(m)
    return m_inv @ ((1.0 / g1 + 1.0 / g2) * m + d / (g1 * g2)) @ m_inv / (2.0 * b * t)


def test_coupled_stein_sigmas_equal_the_explicit_covariance() -> None:
    rng = np.random.default_rng(3)
    for _ in range(40):
        b, t = float(rng.uniform(1e3, 1e6)), float(rng.uniform(0.01, 2.0))
        rms = float(b / math.sqrt(12.0) * rng.uniform(0.05, 1.0))
        g1, g2 = 10.0 ** rng.uniform(-2.0, 3.0, 2)
        rho = float(rng.uniform(-0.95, 0.95))
        cov = _explicit_covariance(b, rms, t, g1, g2, rho)
        s_t, s_f = crlb.stein_sigmas(b, rms, t, g1, g2, rho_tf=rho)
        assert s_t == pytest.approx(math.sqrt(cov[0, 0]), rel=1e-9)
        assert s_f == pytest.approx(math.sqrt(cov[1, 1]), rel=1e-9)
        # The same from the measured quantities: C, the signal-times-noise share s and
        # the ratio F of the incoherent to the signal RMS bandwidth (F^2 = s + q (1 - s)).
        c = g1 * g2 / ((1.0 + g1) * (1.0 + g2))
        s = (1.0 / g1 + 1.0 / g2) / (1.0 / g1 + 1.0 / g2 + 1.0 / (g1 * g2))
        q = max(1.0, b * b / (12.0 * rms * rms))
        f_ratio = math.sqrt(s + q * (1.0 - s))
        m_t, m_f = crlb.sigmas_from_coherence(c, b, rms, t, f_ratio * rms, rho_tf=rho, signal_noise_share=s)
        assert m_t == pytest.approx(s_t, rel=1e-9) and m_f == pytest.approx(s_f, rel=1e-9)
        # rho = 0 is the plain MATH.md section 8 formula.
        p_t, p_f = crlb.stein_sigmas(b, rms, t, g1, g2)
        assert p_t == pytest.approx(math.sqrt((1 / g1 + 1 / g2 + q / (g1 * g2)) / (2 * b * t)) / (2 * math.pi * rms), rel=1e-9)
        assert p_f == pytest.approx(math.sqrt((1 / g1 + 1 / g2 + 1 / (g1 * g2)) / (2 * b * t)) / (math.pi * t / math.sqrt(3)), rel=1e-9)


def test_unknown_share_gives_the_largest_sigma_any_snr_split_can_have() -> None:
    """signal_noise_share=None: the worst case over every (g1, g2) with the measured coherence."""
    b, rms, t = 31250.0, 31250.0 / math.sqrt(12.0), 0.25
    for c, rho in [(0.01, 0.5), (0.2, 0.8), (0.6, -0.3)]:
        # every split of C = x1 x2 with x = g / (1 + g) in (C, 1)
        x1 = np.linspace(c * 1.0001, 0.9999, 4001)
        x2 = c / x1
        g1, g2 = x1 / (1.0 - x1), x2 / (1.0 - x2)
        worst = np.max(crlb.stein_sigmas(b, rms, t, g1, g2, rho_tf=rho)[1])
        assumed = crlb.sigmas_from_coherence(c, b, rms, t, rho_tf=rho)[1]
        assert assumed == pytest.approx(float(worst), rel=1e-4)
        one_clean = crlb.sigmas_from_coherence(c, b, rms, t, rho_tf=rho, signal_noise_share=1.0)[1]
        plain = crlb.sigmas_from_coherence(c, b, rms, t)[1]
        assert one_clean == pytest.approx(plain / math.sqrt(1.0 - rho * rho), rel=1e-9)  # MATH.md section 6 item 4


# --------------------------------------------------------------- Monte Carlo (slow)


def _mc_noise_like(snr_a: float, snr_b: float, trials: int, seed0: int) -> dict[str, float]:
    n, bw = 8192, 25e3
    tdoa, fdoa = -1.3137e-3, 12.345
    t = np.arange(n) / FS
    rows = []
    for i in range(trials):
        rng = np.random.default_rng(seed0 + i)
        src = Process(seed0 + 10**6 + i, int(sfft.next_fast_len(n + 2000)), FS, bw)
        ya = src.at(-1e-3 - tdoa, n) * np.exp(2j * np.pi * (fdoa + 2.0) * t) + cnoise(rng, n, 10.0 ** (-snr_a / 10.0))
        yb = src.at(-1e-3, n) * np.exp(2j * np.pi * 2.0 * t) + cnoise(rng, n, 10.0 ** (-snr_b / 10.0))
        r = caf_estimate(ya, yb, FS, delay_window=(-2.3e-3, -0.3e-3), doppler_window=(-40.0, 40.0), signal_bw_hz=bw)
        if r.tdoa_s is not None:
            rows.append((r.tdoa_s - tdoa, r.fdoa_hz - fdoa, r.sigma_tdoa_s, r.sigma_fdoa_hz, r.rho_tf, r.snr_db,
                         r.integration_s, r.noise_bandwidth_hz))
    a = np.array(rows)
    b_n, t_int = float(a[:, 7].mean()), float(a[:, 6].mean())
    g1, g2 = 10.0 ** (snr_a / 10.0) * FS / b_n, 10.0 ** (snr_b / 10.0) * FS / b_n
    rms = bw / math.sqrt(12.0)
    q = b_n**2 / (12.0 * rms**2)
    stein_t = math.sqrt((1 / g1 + 1 / g2 + q / (g1 * g2)) / (2 * b_n * t_int)) / (2 * math.pi * rms)
    stein_f = math.sqrt((1 / g1 + 1 / g2 + 1 / (g1 * g2)) / (2 * b_n * t_int)) / (math.pi * t_int / math.sqrt(3))
    rho = b_n * t_int * g1 * g2 / ((1 + g1) * (1 + g2))
    return dict(
        n=float(len(rows)), mc_t=a[:, 0].std() / stein_t, mc_f=a[:, 1].std() / stein_f,
        rep_t=np.median(a[:, 2]) / a[:, 0].std(), rep_f=np.median(a[:, 3]) / a[:, 1].std(),
        z_t=float(np.std(a[:, 0] / a[:, 2])), z_f=float(np.std(a[:, 1] / a[:, 3])),
        bias_t=abs(a[:, 0].mean()) / (a[:, 0].std() / math.sqrt(len(rows))),
        bias_f=abs(a[:, 1].mean()) / (a[:, 1].std() / math.sqrt(len(rows))),
        snr=float(a[:, 5].mean()), snr_pred=10.0 * math.log10(rho), rho_std=float(a[:, 4].std()),
        rho_mean_se=abs(float(a[:, 4].mean())) / (float(a[:, 4].std()) / math.sqrt(len(rows))),
        # measurement scatter of the curvature, plus the random time-frequency correlation of
        # one noise realisation of the waveform (of order 1 / sqrt(B T), dominant at high SNR)
        rho_std_pred=math.sqrt(1.0 / (2.0 * rho) + 1.0 / (b_n * t_int)),
    )


@pytest.mark.slow
@pytest.mark.parametrize(
    "snr_a, snr_b, seed0, mc_hi",
    [(-12.0, -12.0, 70_000, 1.22), (10.0, 10.0, 71_000, 1.15), (20.0, -10.0, 72_000, 1.15)],
    ids=["15 dB post-correlation", "37 dB", "20 dB against -10 dB"],
)
def test_monte_carlo_noise_like_at_untested_snrs(snr_a: float, snr_b: float, seed0: int, mc_hi: float) -> None:
    """Near threshold, high SNR, and very unequal channels (200 trials each, negative TDOA).

    Bounds are about 3 standard errors of a 200-trial standard deviation (5 %); nine
    independent sets of the unequal case gave FDOA scatter ratios spread by 0.04 about 1.03.
    """
    m = _mc_noise_like(snr_a, snr_b, 200, seed0)
    assert m["n"] >= 190
    assert m["snr"] == pytest.approx(m["snr_pred"], abs=0.6)
    # Scatter against the closed form (near threshold the CRLB is not quite reached).
    assert 0.85 < m["mc_t"] < mc_hi and 0.85 < m["mc_f"] < mc_hi
    # Reported sigmas against the scatter.
    assert 0.85 < m["rep_t"] < 1.15 and 0.85 < m["rep_f"] < 1.15
    assert 0.85 < m["z_t"] < 1.15 and 0.85 < m["z_f"] < 1.15
    assert m["bias_t"] < 3.5 and m["bias_f"] < 3.5
    # rho_tf of a noise-like signal is scatter only: mean 0, std about 1 / sqrt(2 rho).
    assert m["rho_mean_se"] < 3.5
    assert 0.6 * m["rho_std_pred"] < m["rho_std"] < 1.35 * m["rho_std_pred"]


def _mc_chirp(sweeps: float, snr_a: float, snr_b: float, trials: int, seed0: int) -> dict[str, float]:
    n, bw = 4096, 20e3
    t = np.arange(n) / FS
    period = n / FS / sweeps
    tdoa, fdoa = 12.37 / FS, 3.0
    rows = []
    for i in range(trials):
        rng = np.random.default_rng(seed0 + i)
        ya = sawtooth(t - tdoa, bw, period) * np.exp(2j * np.pi * fdoa * t) + cnoise(rng, n, 10.0 ** (-snr_a / 10.0))
        yb = sawtooth(t, bw, period) + cnoise(rng, n, 10.0 ** (-snr_b / 10.0))
        r = caf_estimate(
            ya, yb, FS, delay_window=(tdoa - 20e-6, tdoa + 20e-6), doppler_window=(fdoa - 0.4 / period, fdoa + 0.4 / period)
        )
        if r.tdoa_s is not None:
            rows.append((r.tdoa_s - tdoa, r.fdoa_hz - fdoa, r.sigma_tdoa_s, r.sigma_fdoa_hz, r.rho_tf, r.integration_s))
    a = np.array(rows)
    n_sum = int(round(float(a[:, 5].mean()) * FS))
    rho, rms = sawtooth_moments((n - n_sum) / 2 / FS, n_sum / FS, bw, period)
    g1, g2 = 10.0 ** (snr_a / 10.0), 10.0 ** (snr_b / 10.0)  # no filter: B = fs, unit-power sweep
    cov = _explicit_covariance(FS, rms, n_sum / FS, g1, g2, rho)
    plain = _explicit_covariance(FS, rms, n_sum / FS, g1, g2, 0.0)
    k = 1.0 / math.sqrt(1.0 - rho * rho)
    return dict(
        n=float(len(rows)), rho=rho, rho_tf=float(a[:, 4].mean()),
        corr=float(np.corrcoef(a[:, 0], a[:, 1])[0, 1]), corr_model=cov[0, 1] / math.sqrt(cov[0, 0] * cov[1, 1]),
        mc_t=a[:, 0].std() / math.sqrt(cov[0, 0]), mc_f=a[:, 1].std() / math.sqrt(cov[1, 1]),
        math_t=a[:, 0].std() / (math.sqrt(plain[0, 0]) * k), math_f=a[:, 1].std() / (math.sqrt(plain[1, 1]) * k),
        rep_t=np.median(a[:, 2]) / a[:, 0].std(), rep_f=np.median(a[:, 3]) / a[:, 1].std(),
        z_t=float(np.std(a[:, 0] / a[:, 2])), z_f=float(np.std(a[:, 1] / a[:, 3])),
    )


@pytest.mark.slow
@pytest.mark.parametrize(
    "sweeps, snr_a, snr_b, seed0, math_low",
    [(2.0, -3.0, -3.0, 60_000, True), (2.0, 25.0, -5.0, 61_000, False), (1.1, -3.0, -3.0, 62_000, True)],
    ids=["rho 0.48 equal SNRs", "rho 0.48 one clean channel", "rho 0.59 equal SNRs"],
)
def test_monte_carlo_coupled_sweep_sigmas(sweeps: float, snr_a: float, snr_b: float, seed0: int, math_low: bool) -> None:
    """Delay-Doppler coupled waveform (sawtooth, rho = 1/m-like): scatter, error correlation, reported sigmas.

    The scatter follows the explicit covariance with both noise terms; MATH.md's
    1 / sqrt(1 - rho^2) rule (exact with one clean channel) is too small when both
    channels are noisy. The reported sigmas follow the scatter (250 trials each).
    """
    m = _mc_chirp(sweeps, snr_a, snr_b, 250, seed0)
    assert m["n"] >= 240
    assert m["rho_tf"] == pytest.approx(m["rho"], abs=0.02)
    assert 0.87 < m["mc_t"] < 1.14 and 0.87 < m["mc_f"] < 1.14  # 3 standard errors (4.5 %)
    assert m["corr"] == pytest.approx(m["corr_model"], abs=0.08)
    if math_low:
        assert m["math_f"] > 1.12  # the sqrt(k) rule understates the Doppler scatter
    else:
        assert 0.87 < m["math_t"] < 1.14 and 0.87 < m["math_f"] < 1.14
    assert 0.85 < m["rep_t"] < 1.15 and 0.85 < m["rep_f"] < 1.15
    assert 0.85 < m["z_t"] < 1.18 and 0.85 < m["z_f"] < 1.18
