"""Tests of the cross-ambiguity estimator (MATH.md section 6) against independent calculations.

Test signals are made here, not by the simulator: band-limited complex Gaussian noise
is drawn in the frequency domain at four times the capture rate, each path is delayed
by an FFT phase ramp (exact fractional delay of the periodic waveform), shifted in
frequency, and decimated by taking every fourth sample (nothing can alias, the band is
empty above the capture Nyquist). Independent white noise is added per channel.
"""
from __future__ import annotations

import math
import time

import numpy as np
import pytest
from scipy import fft as sfft

from petal_locate.core import caf as caf_mod
from petal_locate.core import crlb
from petal_locate.core.caf import CafResult, caf_estimate

FS = 31250.0
BW = 25e3
RMS_FLAT = BW / math.sqrt(12.0)


# ----------------------------------------------------------------------------- signals


class Source:
    """One emitter waveform s(t), unit mean power, flat over `bw` Hz, periodic.

    `path(delay, f_off, late)` returns the capture ``s(t - delay) exp(j 2 pi f_off t)``
    at ``t = late + m / fs``, m = 0..n-1. The period is longer than the capture plus
    every delay used, so no wrap-around enters a capture.
    """

    def __init__(self, seed: int, n: int, fs: float, bw: float, pad_s: float = 0.045, over: int = 4) -> None:
        rng = np.random.default_rng(seed)
        self.n, self.fs, self.over = n, fs, over
        n_period = sfft.next_fast_len(n + int(math.ceil(pad_s * fs)))
        self.n_hi = n_period * over
        self.f = sfft.fftfreq(self.n_hi, 1.0 / (fs * over))
        inband = np.abs(self.f) <= bw / 2.0
        spec = np.zeros(self.n_hi, dtype=complex)
        k = int(np.count_nonzero(inband))
        spec[inband] = rng.standard_normal(k) + 1j * rng.standard_normal(k)
        self.spec = spec * (self.n_hi / math.sqrt(float(np.sum(np.abs(spec) ** 2))))
        self.t_hi = np.arange(self.n_hi) / (fs * over)

    def path(self, delay: float, f_off: float, late: float = 0.0) -> np.ndarray:
        x = sfft.ifft(self.spec * np.exp(-2j * np.pi * self.f * (delay - late)))
        x = x * np.exp(2j * np.pi * f_off * (self.t_hi + late))
        return x[:: self.over][: self.n]


def white(seed: int, n: int, power: float = 1.0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return math.sqrt(power / 2.0) * (rng.standard_normal(n) + 1j * rng.standard_normal(n))


D_B = 1.23456e-3  # delay of the reference path, s (not on the sample grid)
F_B = 3.7  # baseband offset of channel B, Hz


def noise_pair(
    seed: int,
    n: int,
    tdoa: float,
    fdoa: float,
    snr_a_db: float | None,
    snr_b_db: float | None,
    *,
    fs: float = FS,
    bw: float = BW,
    a_late: float = 0.0,
    b_late: float = 0.0,
    pad_s: float = 0.045,
) -> tuple[np.ndarray, np.ndarray]:
    """Channel pair with true TDOA / FDOA (A minus B). SNRs in dB over the whole capture band fs."""
    src = Source(seed, n, fs, bw, pad_s)
    ya = src.path(D_B + tdoa, F_B + fdoa, a_late)
    yb = src.path(D_B, F_B, b_late)
    if snr_a_db is not None:
        ya = ya + white(seed + 500_000, n, 10.0 ** (-snr_a_db / 10.0))
    if snr_b_db is not None:
        yb = yb + white(seed + 900_000, n, 10.0 ** (-snr_b_db / 10.0))
    return ya, yb


def chirp(t: np.ndarray, bw: float, period: float) -> np.ndarray:
    """Sawtooth linear FM sweeping -bw/2 .. +bw/2 every `period` seconds."""
    u = np.mod(t, period)
    return np.exp(2j * np.pi * (-0.5 * bw * u + 0.5 * (bw / period) * u * u))


def brute_caf(ya: np.ndarray, yb: np.ndarray, fs: float, lag: int, nu: float) -> complex:
    """chi at an integer array lag straight from the definition (zero outside the capture)."""
    n = np.arange(yb.size)
    m = n + lag
    ok = (m >= 0) & (m < ya.size)
    return complex(np.sum(ya[m[ok]] * np.conj(yb[n[ok]]) * np.exp(-2j * np.pi * nu * n[ok] / fs)))


WIDE = dict(delay_window=(-3e-3, 37e-3), doppler_window=(-200.0, 200.0), signal_bw_hz=BW)


# ------------------------------------------------------------------- basic estimation


def test_recovers_tdoa_fdoa_far_from_zero_lag() -> None:
    tdoa, fdoa = 17.3e-3, -63.0
    ya, yb = noise_pair(1, 31250, tdoa, fdoa, 0.0, 0.0)
    r = caf_estimate(ya, yb, FS, **WIDE)
    assert r.detected and r.reason is None and not r.mirror_ambiguous
    assert r.tdoa_s is not None and r.fdoa_hz is not None
    assert r.sigma_tdoa_s is not None and r.sigma_fdoa_hz is not None

    # Stein bound from the known input SNRs, in the estimator's correlation bandwidth.
    # B: the 25 kHz filter (plus Doppler reach and transition). T: the samples of B
    # symmetric about its midpoint that have a partner in A, 2 x 17.3 ms short of 1 s
    # (less 3 ms of filter transient and the trim to a fast FFT length).
    b_n, t_int = r.noise_bandwidth_hz, r.integration_s
    assert 25e3 < b_n < 27.5e3 and abs(t_int - (1.0 - 2.0 * 17.3e-3)) < 8e-3
    g = FS / b_n  # 0 dB over fs, all of the signal inside B
    s_t, s_f = crlb.stein_sigmas(b_n, RMS_FLAT, t_int, g, g)
    assert abs(r.tdoa_s - tdoa) < 4.0 * s_t
    assert abs(r.fdoa_hz - fdoa) < 4.0 * s_f
    assert 150e-9 < s_t < 200e-9  # the scale of the problem: a fifth of a microsecond
    assert r.sigma_tdoa_s == pytest.approx(s_t, rel=0.15)
    assert r.sigma_fdoa_hz == pytest.approx(s_f, rel=0.15)
    # Post-correlation SNR and coherence against their closed forms.
    assert r.snr_db == pytest.approx(10.0 * math.log10(crlb.post_correlation_snr(b_n, t_int, g, g)), abs=0.5)
    assert r.coherence == pytest.approx(crlb.coherence(g, g), rel=0.05)
    assert r.rms_bandwidth_hz == pytest.approx(RMS_FLAT, rel=0.03)
    assert abs(r.rho_tf) < 0.1
    assert len(r.candidates) == 1
    assert r.candidates[0] == pytest.approx((r.tdoa_s, r.fdoa_hz, r.snr_db))


def test_swapping_channels_flips_both_signs() -> None:
    tdoa, fdoa = 17.3e-3, -63.0
    ya, yb = noise_pair(2, 31250, tdoa, fdoa, 0.0, 0.0)
    r_ab = caf_estimate(ya, yb, FS, **WIDE)
    r_ba = caf_estimate(yb, ya, FS, delay_window=(-37e-3, 3e-3), doppler_window=(-200.0, 200.0), signal_bw_hz=BW)
    assert r_ab.tdoa_s is not None and r_ba.tdoa_s is not None
    assert r_ab.tdoa_s > 0.0 > r_ba.tdoa_s and r_ab.fdoa_hz < 0.0 < r_ba.fdoa_hz
    # Same data, so the two estimates agree well inside one sigma (not exactly: each
    # is centred on the midpoint of its own channel B, 17.3 ms apart, see section 5).
    assert abs(r_ab.tdoa_s + r_ba.tdoa_s) < 0.5 * r_ab.sigma_tdoa_s
    assert abs(r_ab.fdoa_hz + r_ba.fdoa_hz) < 0.5 * r_ab.sigma_fdoa_hz
    assert abs(r_ab.tdoa_s - tdoa) < 4.0 * r_ab.sigma_tdoa_s
    assert r_ba.snr_db == pytest.approx(r_ab.snr_db, abs=0.2)


def test_late_start_of_either_channel_leaves_tdoa_unchanged() -> None:
    """MATH.md section 12 item 5: A 0.38 ms late, or B 0.52 ms late, with t0_offset_s set."""
    tdoa, fdoa, n = 17.3e-3, -63.0, 16384
    sigma_30db = crlb.sigmas_from_coherence(1000.0 / (26.3e3 * n / FS), 26.3e3, RMS_FLAT, n / FS)[0]
    base = caf_estimate(*noise_pair(3, n, tdoa, fdoa, None, None), FS, **WIDE)
    a_late = caf_estimate(*noise_pair(3, n, tdoa, fdoa, None, None, a_late=0.38e-3), FS, t0_offset_s=0.38e-3, **WIDE)
    b_late = caf_estimate(*noise_pair(3, n, tdoa, fdoa, None, None, b_late=0.52e-3), FS, t0_offset_s=-0.52e-3, **WIDE)
    for r in (base, a_late, b_late):
        assert r.tdoa_s is not None
        assert abs(r.tdoa_s - tdoa) < 0.02 * sigma_30db  # noise-free copies: nanoseconds
        assert abs(r.fdoa_hz - fdoa) < 1e-3
    assert abs(a_late.tdoa_s - base.tdoa_s) < 0.02 * sigma_30db
    assert abs(b_late.tdoa_s - base.tdoa_s) < 0.02 * sigma_30db
    # Without the offset the estimate moves by exactly the late start (array lag).
    wrong = caf_estimate(*noise_pair(3, n, tdoa, fdoa, None, None, a_late=0.38e-3), FS, **WIDE)
    assert wrong.tdoa_s == pytest.approx(tdoa - 0.38e-3, abs=0.02 * sigma_30db)
    # Axes are true A-minus-B delays too.
    assert a_late.delay_cut[0][0] == pytest.approx(base.delay_cut[0][0], abs=1.0 / FS)
    assert a_late.delay_axis_s.min() <= tdoa <= a_late.delay_axis_s.max()


def test_drifting_delay_and_doppler_are_reported_at_the_section_5_epochs() -> None:
    """TDOA = D_A(t_mid + TDOA) - D_B(t_mid), FDOA = f_A(t_mid + TDOA) - f_B(t_mid), t_mid = middle of B.

    Both delays and both Dopplers drift linearly over the capture (half the allowed
    0.5 / B and 1 / T). Summing over every overlapping sample would centre A and B
    half a lag either side of those epochs and miss by (rate difference) x TDOA / 2:
    173 ns and 16 mHz here.
    """
    from scipy.signal import czt

    n, tau0 = 16384, 17.3e-3
    rate_a, rate_b = 1e-5, -1e-5  # d(delay)/dt, s/s
    f_a0, f_b0, alpha_a, alpha_b = -59.3, 3.7, 0.9, -0.9  # Hz and Hz/s
    t = np.arange(n) / FS
    t_mid = (n - 1) / (2.0 * FS)
    tdoa = (tau0 + (rate_a - rate_b) * t_mid) / (1.0 - rate_a)  # solves tau = D_A(t_mid + tau) - D_B(t_mid)
    fdoa = f_a0 + alpha_a * (t_mid + tdoa) - (f_b0 + alpha_b * t_mid)

    def path(src: Source, d0: float, rate: float, f0: float, alpha: float) -> np.ndarray:
        # s((1 - rate) t - d0): the Fourier series of the periodic source on a
        # stretched time grid, by a chirp-z transform over its occupied bins.
        k = np.flatnonzero(src.spec)
        k = k[np.argsort(src.f[k])]
        f_k, step = src.f[k], src.fs * src.over / src.n_hi
        coef = src.spec[k] * np.exp(-2j * np.pi * f_k * d0) / src.n_hi
        env = czt(coef, m=n, w=np.exp(2j * np.pi * step * (1.0 - rate) / FS))
        env = env * np.exp(2j * np.pi * f_k[0] * (1.0 - rate) * t)
        for i in (0, 1, n // 3, n - 1):  # the generator against direct summation
            direct = np.sum(src.spec[k] * np.exp(2j * np.pi * f_k * ((1.0 - rate) * t[i] - d0))) / src.n_hi
            assert abs(env[i] - direct) < 1e-7
        return env * np.exp(2j * np.pi * (f0 * t + 0.5 * alpha * t * t))

    err_t, err_f = [], []
    for seed in range(4):
        src = Source(900 + seed, n, FS, BW, pad_s=0.03)
        ya = path(src, D_B + tau0, rate_a, f_a0, alpha_a)
        yb = path(src, D_B, rate_b, f_b0, alpha_b)
        r = caf_estimate(ya, yb, FS, delay_window=(12e-3, 22e-3), doppler_window=(-100.0, -30.0), signal_bw_hz=BW)
        assert r.reason is None
        err_t.append(r.tdoa_s - tdoa)
        err_f.append(r.fdoa_hz - fdoa)
    half_lag_t = abs(rate_a - rate_b) * tdoa / 2.0
    half_lag_f = abs(alpha_a - alpha_b) * tdoa / 2.0
    assert half_lag_t == pytest.approx(173e-9, rel=0.02) and half_lag_f == pytest.approx(15.6e-3, rel=0.02)
    # Residual: the random signal weights the drift unevenly (about 40 ns, 3 mHz a capture).
    assert abs(float(np.mean(err_t))) < 0.3 * half_lag_t
    assert abs(float(np.mean(err_f))) < 0.3 * half_lag_f


def test_late_start_with_noise_stays_within_sigma() -> None:
    tdoa, fdoa, n = 17.3e-3, -63.0, 31250
    for kw, t0 in ((dict(a_late=0.38e-3), 0.38e-3), (dict(b_late=0.52e-3), -0.52e-3)):
        ya, yb = noise_pair(4, n, tdoa, fdoa, -3.0, -3.0, **kw)
        r = caf_estimate(ya, yb, FS, t0_offset_s=t0, **WIDE)
        assert r.tdoa_s is not None
        assert abs(r.tdoa_s - tdoa) < 4.0 * r.sigma_tdoa_s
        assert abs(r.fdoa_hz - fdoa) < 4.0 * r.sigma_fdoa_hz


@pytest.mark.parametrize("signal_bw, bw", [(BW, BW), (None, BW), (None, 0.999 * FS)])
def test_fine_search_bias_below_5_percent_of_crlb_at_30_db(signal_bw: float | None, bw: float) -> None:
    """Noise-free copies at delays and Dopplers off the search grids: the estimator's own bias."""
    n = 8192
    t_int = n / FS
    b_n = 26.3e3 if signal_bw else FS
    c_30db = 1000.0 / (b_n * t_int)  # coherence that gives 30 dB post-correlation SNR
    s_t, s_f = crlb.sigmas_from_coherence(c_30db, b_n, bw / math.sqrt(12.0), t_int)
    cases = [(0.0, 0.0), (0.13, 0.2), (0.25, 0.37), (0.37, 0.5), (0.5, 0.11), (0.77, 0.81)]
    for i, (frac_lag, frac_bin) in enumerate(cases):
        tdoa = (40.0 + frac_lag) / FS
        fdoa = -11.0 + frac_bin * 0.25 * FS / n
        ya, yb = noise_pair(100 + i, n, tdoa, fdoa, None, None, bw=bw)
        r = caf_estimate(ya, yb, FS, delay_window=(0.5e-3, 2.5e-3), doppler_window=(-40.0, 40.0), signal_bw_hz=signal_bw)
        assert r.tdoa_s is not None and r.reason is None
        assert abs(r.tdoa_s - tdoa) < 0.05 * s_t
        assert abs(r.fdoa_hz - fdoa) < 0.05 * s_f
        assert r.coherence > 0.995


def test_coarse_surface_matches_brute_force_definition() -> None:
    """Segment correlation plus Doppler FFT against the CAF summed straight from its definition."""
    n, fs = 2048, 8000.0
    lag_true, nu_true = 717, 72.0
    yb = white(12, n - 100)  # unequal lengths: zero fill at the end
    ya = 0.5 * white(11, n)
    m = np.arange(lag_true, n - 100)
    ya[m] += yb[m - lag_true] * np.exp(2j * np.pi * nu_true * m / fs)  # delayed, Doppler-shifted copy
    lag0, n_lags, nu_lo, nu_hi = 700, 40, 30.0, 90.0  # window far from zero lag, off-centre Doppler
    co = caf_mod._coarse_surface(ya, yb, fs, lag0, n_lags, nu_lo, nu_hi, 0.0)
    assert co.power.shape[0] == n_lags and co.lag0 == lag0 and co.nu_c == pytest.approx(60.0)
    seg = max(1, min(int(0.25 * fs / 30.0), yb.size // 64))
    assert co.seg == seg and 30.0 * seg / fs <= 0.25  # MATH.md section 6 item 1
    col_nu = co.nu_c + (co.bin0 + np.arange(co.power.shape[1])) * co.dnu
    assert col_nu[0] <= nu_lo and col_nu[-1] >= nu_hi and co.dnu <= fs / (4.0 * yb.size)  # padded to 4 K

    def block_sum(lag: int, nu: float) -> tuple[complex, int]:
        # What the search does, with explicit loops: remove the window centre, sum
        # each B segment against A at this lag, apply the Doppler phase once per segment.
        # Also counts the products that exist (both samples inside their captures).
        total, count = 0.0 + 0.0j, 0
        for k in range(-(-yb.size // seg)):
            acc = 0.0 + 0.0j
            for i in range(k * seg, min((k + 1) * seg, yb.size)):
                j = i + lag
                if 0 <= j < ya.size:
                    acc += ya[j] * np.exp(-2j * np.pi * co.nu_c * j / fs) * np.conj(yb[i])
                    count += 1
            total += acc * np.exp(-2j * np.pi * (nu - co.nu_c) * k * seg / fs)
        return total, count

    def cell(lag: int, nu: float) -> float:
        # Every row of the surface is scaled to the same number of products (the length
        # of B), so that its noise floor does not depend on the lag (see caf._Coarse):
        # |segment sum|^2 * len(yb) / (products at this lag).
        total, count = block_sum(lag, nu)
        return abs(total) ** 2 * yb.size / count

    row_pk, col_pk = np.unravel_index(int(np.argmax(co.power)), co.power.shape)
    assert lag0 + row_pk == lag_true and abs(col_nu[col_pk] - nu_true) <= 0.5 * co.dnu
    rng = np.random.default_rng(5)
    cells = [(int(row_pk), int(col_pk)), (int(row_pk), 0), (0, int(col_pk))]
    cells += [(int(rng.integers(0, n_lags)), int(rng.integers(0, col_nu.size))) for _ in range(9)]
    for row, col in cells:
        assert co.power[row, col] == pytest.approx(cell(lag0 + row, float(col_nu[col])), rel=2e-3)
    # The scale at the peak row: A ends 1331 samples after lag 717, B has 1948 samples.
    overlap = min(yb.size, ya.size - lag_true)
    assert overlap == 1331 and block_sum(lag_true, 0.0)[1] == overlap
    # Against the exact CAF the segment sums cost a coherent peak sinc^2(nu' L / fs) at most:
    # 0.2 dB here, and 0.9 dB at the edge of any window by the choice of L.
    exact = abs(brute_caf(ya, yb, fs, lag_true, float(col_nu[col_pk]))) ** 2
    loss = np.sinc((col_nu[col_pk] - co.nu_c) * seg / fs) ** 2
    assert co.power[row_pk, col_pk] * overlap / yb.size / exact == pytest.approx(loss, rel=0.03)
    assert exact == pytest.approx(float(yb.size - lag_true) ** 2, rel=0.1)  # N^2 for unit-power copies
    # The half-lag statistic of the pair of lags around the peak was collected too.
    assert np.any((co.half_row == row_pk) & (co.half_col == col_pk))


def test_peak_power_and_floor_match_definition() -> None:
    """snr_db is the exact |chi|^2 at the peak over the mean off-peak |chi|^2, both from the definition."""
    n, tdoa, fdoa = 4096, 40.0 / FS, -11.0  # integer lag so the definition can be evaluated directly
    ya, yb = noise_pair(21, n, tdoa, fdoa, -3.0, -3.0)
    r = caf_estimate(ya, yb, FS, delay_window=(0.5e-3, 2.5e-3), doppler_window=(-40.0, 40.0))
    assert r.tdoa_s is not None and abs(r.tdoa_s - tdoa) < 0.2 / FS
    a = ya / np.sqrt(np.mean(np.abs(ya) ** 2))
    b = yb / np.sqrt(np.mean(np.abs(yb) ** 2))
    peak = abs(brute_caf(a, b, FS, 40, r.fdoa_hz)) ** 2
    rng = np.random.default_rng(3)
    off = [
        abs(brute_caf(a, b, FS, int(rng.integers(100, 400)), float(rng.uniform(-200.0, 200.0)))) ** 2
        for _ in range(400)
    ]
    snr_brute = 10.0 * math.log10(peak / float(np.mean(off)))
    assert r.snr_db == pytest.approx(snr_brute, abs=0.6)
    # The estimate itself uses the B samples symmetric about B's midpoint: [40, n - 40).
    lag_i = 40
    sel = np.arange(lag_i, n - lag_i)
    chi = np.sum(a[sel + lag_i] * np.conj(b[sel]) * np.exp(-2j * np.pi * r.fdoa_hz * sel / FS))
    coh_brute = abs(chi) ** 2 / (np.sum(np.abs(a[sel + lag_i]) ** 2) * np.sum(np.abs(b[sel]) ** 2))
    assert r.coherence == pytest.approx(float(coh_brute), rel=0.02)
    assert r.integration_s == pytest.approx(sel.size / FS, rel=0.01)
    assert r.noise_bandwidth_hz == FS


# ------------------------------------------------------------------------- statistics


def _monte_carlo(
    n: int, trials: int, snr_db: float, bw: float, signal_bw: float | None, seed0: int
) -> dict[str, float]:
    tdoa, fdoa = 1.3137e-3, 12.345
    e_t, e_f, s_t, s_f, b_n, t_int, snr = [], [], [], [], [], [], []
    for i in range(trials):
        ya, yb = noise_pair(seed0 + i, n, tdoa, fdoa, snr_db, snr_db, bw=bw, pad_s=0.01)
        r = caf_estimate(ya, yb, FS, delay_window=(0.3e-3, 2.3e-3), doppler_window=(-40.0, 40.0), signal_bw_hz=signal_bw)
        assert r.tdoa_s is not None and r.reason is None, (i, r.reason, r.snr_db)
        e_t.append(r.tdoa_s - tdoa)
        e_f.append(r.fdoa_hz - fdoa)
        s_t.append(r.sigma_tdoa_s)
        s_f.append(r.sigma_fdoa_hz)
        b_n.append(r.noise_bandwidth_hz)
        t_int.append(r.integration_s)
        snr.append(r.snr_db)
    et, ef = np.array(e_t), np.array(e_f)
    b_mean, t_mean = float(np.mean(b_n)), float(np.mean(t_int))
    g = 10.0 ** (snr_db / 10.0) * FS / b_mean  # input SNR in the correlation bandwidth
    stein_t, stein_f = crlb.stein_sigmas(b_mean, bw / math.sqrt(12.0), t_mean, g, g)
    plain_t = crlb.sigmas_from_coherence(crlb.coherence(g, g), b_mean, bw / math.sqrt(12.0), t_mean)[0]
    return dict(
        std_t=float(et.std()), std_f=float(ef.std()), mean_t=float(et.mean()), mean_f=float(ef.mean()),
        stein_t=float(stein_t), stein_f=float(stein_f), plain_t=float(plain_t),
        z_t=float(np.std(et / np.array(s_t))), z_f=float(np.std(ef / np.array(s_f))),
        med_sigma_t=float(np.median(s_t)), med_sigma_f=float(np.median(s_f)),
        snr=float(np.mean(snr)), snr_pred=10.0 * math.log10(crlb.post_correlation_snr(b_mean, t_mean, g, g)),
        trials=float(trials),
    )


@pytest.mark.slow
@pytest.mark.parametrize("snr_db, seed0", [(-10.0, 40_000), (-3.0, 20_000)])
def test_monte_carlo_scatter_matches_stein_bound(snr_db: float, seed0: int) -> None:
    """250 seeded trials per input SNR (18.9 dB and 29.7 dB post-correlation)."""
    m = _monte_carlo(8192, 250, snr_db, BW, BW, seed0)
    assert m["snr"] == pytest.approx(m["snr_pred"], abs=0.5)
    # Empirical scatter against the Stein bound (standard error of a 250-trial sigma: 4.5 %).
    assert m["std_t"] == pytest.approx(m["stein_t"], rel=0.2)
    assert m["std_f"] == pytest.approx(m["stein_f"], rel=0.2)
    # Reported sigmas against the scatter: typical value and normalised errors.
    assert m["med_sigma_t"] == pytest.approx(m["std_t"], rel=0.2)
    assert m["med_sigma_f"] == pytest.approx(m["std_f"], rel=0.2)
    assert 0.8 < m["z_t"] < 1.25 and 0.8 < m["z_f"] < 1.25
    # No bias: mean error within 3.5 standard errors.
    assert abs(m["mean_t"]) < 3.5 * m["std_t"] / math.sqrt(m["trials"])
    assert abs(m["mean_f"]) < 3.5 * m["std_f"] / math.sqrt(m["trials"])


@pytest.mark.slow
def test_monte_carlo_signal_narrower_than_band() -> None:
    """A 10 kHz signal in a 31.25 kHz capture, correlated over the full band and over its own.

    Without signal_bw_hz the noise-times-noise term is collected over the whole
    capture band: the scatter follows the q term of MATH.md section 8 (2.6 times the
    plain formula here) and the reported sigma follows the scatter. With it, the
    scatter drops to the bound for the filtered band.
    """
    bw = 10e3
    wide = _monte_carlo(8192, 200, -6.0, bw, None, 60_000)
    assert wide["stein_t"] / wide["plain_t"] > 2.0  # the q term matters in this case
    assert wide["std_t"] == pytest.approx(wide["stein_t"], rel=0.2)
    assert wide["med_sigma_t"] == pytest.approx(wide["std_t"], rel=0.2)
    assert 0.8 < wide["z_t"] < 1.25
    assert wide["std_f"] == pytest.approx(wide["stein_f"], rel=0.2)  # FDOA is unaffected by q
    assert 0.8 < wide["z_f"] < 1.25

    narrow = _monte_carlo(8192, 200, -6.0, bw, bw, 60_000)
    assert narrow["std_t"] == pytest.approx(narrow["stein_t"], rel=0.2)
    assert narrow["med_sigma_t"] == pytest.approx(narrow["std_t"], rel=0.2)
    assert 0.8 < narrow["z_t"] < 1.25 and 0.8 < narrow["z_f"] < 1.25
    assert narrow["std_t"] < 0.5 * wide["std_t"]  # band limiting pays


def test_signal_narrower_than_band_single_shot() -> None:
    bw, tdoa, fdoa, n = 10e3, 17.3e-3, -63.0, 31250
    ya, yb = noise_pair(31, n, tdoa, fdoa, -8.0, -8.0, bw=bw)
    plain = caf_estimate(ya, yb, FS, delay_window=(-3e-3, 37e-3), doppler_window=(-200.0, 200.0))
    limited = caf_estimate(ya, yb, FS, delay_window=(-3e-3, 37e-3), doppler_window=(-200.0, 200.0), signal_bw_hz=bw)
    assert plain.noise_bandwidth_hz == FS
    assert 10.4e3 < limited.noise_bandwidth_hz < 11.5e3  # 10 kHz plus 2 x 200 Hz of Doppler plus transition
    for r in (plain, limited):
        assert r.tdoa_s is not None and r.reason is None
        g = 10.0 ** (-0.8) * FS / r.noise_bandwidth_hz
        s_t, s_f = crlb.stein_sigmas(r.noise_bandwidth_hz, bw / math.sqrt(12.0), r.integration_s, g, g)
        assert r.sigma_tdoa_s == pytest.approx(s_t, rel=0.25)
        assert r.sigma_fdoa_hz == pytest.approx(s_f, rel=0.25)
        assert abs(r.tdoa_s - tdoa) < 4.0 * s_t and abs(r.fdoa_hz - fdoa) < 4.0 * s_f
        assert r.rms_bandwidth_hz == pytest.approx(bw / math.sqrt(12.0), rel=0.1)
    assert plain.sigma_tdoa_s > 2.0 * limited.sigma_tdoa_s
    assert limited.snr_db > plain.snr_db + 2.0


def test_no_detection_on_independent_noise() -> None:
    """False alarms at the default 13 dB threshold over 150 trials, and the scale of the noise floor."""
    n, trials = 4096, 150
    snr, detections = [], 0
    for i in range(trials):
        ya, yb = white(7000 + i, n), white(8000 + i, n)
        r = caf_estimate(
            ya, yb, FS, delay_window=(-1e-3, 1e-3), doppler_window=(-50.0, 50.0),
            signal_bw_hz=BW if i % 2 else None,
        )
        snr.append(r.snr_db)
        if r.detected:
            detections += 1
        else:
            assert r.tdoa_s is None and r.fdoa_hz is None and r.reason == caf_mod.REASON_BELOW_THRESHOLD
            assert r.sigma_tdoa_s is None and r.sigma_fdoa_hz is None and r.candidates == []
    # About 800 resolution cells (2 ms x 25 kHz by 100 Hz x 0.13 s): the chance that
    # the maximum of that many exponential cells passes 13 dB (x 20) is of order 1e-4 a trial.
    assert detections <= 1
    # The strongest noise cell sits where the maximum of that many unit-mean
    # exponential cells should: ln(cells) within a factor of a few in the cell count.
    cells = (2e-3 * BW) * (100.0 * n / FS)
    expected_db = 10.0 * math.log10(math.log(4.0 * cells / math.log(2.0)))
    assert float(np.median(snr)) == pytest.approx(expected_db, abs=1.0)
    assert max(snr) < 13.6


def test_noise_floor_follows_the_band_filter_across_doppler() -> None:
    """With a filter not much wider than the Doppler window the floor is not flat.

    The model (|H|^2 correlated with itself, folded by the segment sums) is compared
    with the mean of noise-only surfaces at the window centre, its edge and the edge
    of the Doppler FFT range.
    """
    from scipy import signal as ssignal

    fs, n = 20000.0, 20000
    taps, noise_bw = caf_mod._band_filter(fs, 500.0, n // 8)
    assert 500.0 < noise_bw < 560.0
    shape = caf_mod._FloorShape(taps, fs)
    acc = None
    for seed in range(6):
        a = ssignal.oaconvolve(white(600 + seed, n), taps, mode="valid")
        b = ssignal.oaconvolve(white(650 + seed, n), taps, mode="valid")
        co = caf_mod._coarse_surface(a, b, fs, -400, 800, -100.0, 100.0, 1e9)
        mean = co.stat0.mean(axis=0)
        acc = mean if acc is None else acc + mean
    nu = co.nu_c + co.stat_bins * co.dnu
    model = shape.at(nu, co.nu_c, co.seg)
    assert model[np.argmin(np.abs(nu))] == pytest.approx(1.0, abs=0.01)
    # Closed form for an ideal filter of width B: the product-noise spectrum is the
    # triangle 1 - |nu| / B, and segment sums of length L weight its images at
    # nu + m fs / L by sinc^2(nu L / fs + m).
    step = fs / co.seg
    ideal = sum(
        np.maximum(1.0 - np.abs(nu + m * step) / noise_bw, 0.0) * np.sinc(nu / step + m) ** 2 for m in range(-6, 7)
    )
    assert np.max(np.abs(model - ideal)) < 0.04
    centre = np.abs(nu) < 25.0
    scale = float(acc[centre].mean() / model[centre].mean())
    for lo, hi, expect in ((75.0, 125.0, 0.70), (170.0, 200.0, 0.51)):
        band = (np.abs(nu) >= lo) & (np.abs(nu) <= hi)
        assert float(model[band].mean()) == pytest.approx(expect, abs=0.03)
        assert float(acc[band].mean()) / scale == pytest.approx(float(model[band].mean()), rel=0.12)
    # No filter: flat.
    assert np.all(caf_mod._FloorShape(None, fs).at(nu, 0.0, co.seg) == 1.0)


def test_no_false_alarms_with_a_narrow_band_filter() -> None:
    """300 Hz signal bandwidth, +/- 100 Hz Doppler: a flat floor estimate would sit 1 dB low at the centre."""
    fs, n, snr = 20000.0, 20000, []
    for i in range(30):
        r = caf_estimate(
            white(7600 + i, n), white(7700 + i, n), fs, delay_window=(-20e-3, 20e-3),
            doppler_window=(-100.0, 100.0), signal_bw_hz=300.0,
        )
        assert not r.detected
        snr.append(r.snr_db)
    # About 21 x 200 resolution cells (40 ms x 525 Hz by 200 Hz x 1 s).
    assert float(np.median(snr)) == pytest.approx(10.0 * math.log10(math.log(4.0 * 21 * 200 / math.log(2.0))), abs=0.8)


def test_false_alarm_rate_is_low_in_the_default_uhf_window() -> None:
    """The 40 ms x 400 Hz window of the default geometry holds about 4e5 cells.

    A smoke test only. Measured over 800 noise-only captures (400 with the 25 kHz
    filter, 400 without): 13 detections (1.6 % a capture) at the default 13 dB, all
    below 13.4 dB; none reached 13.5 dB.
    """
    detections = 0
    for i in range(12):
        r = caf_estimate(white(9100 + i, 31250), white(9200 + i, 31250), FS, **WIDE)
        detections += int(r.detected)
        assert 9.5 < r.snr_db < 14.5
    assert detections <= 2


# ------------------------------------------------------------------ narrow-band signals


def _cw_pair(seed: int, n: int, amp_db: float, f_a: float, f_b: float) -> tuple[np.ndarray, np.ndarray]:
    t = np.arange(n) / FS
    amp = 10.0 ** (amp_db / 20.0)
    ya = amp * np.exp(2j * np.pi * f_a * t) + white(seed, n)
    yb = amp * np.exp(2j * np.pi * f_b * t + 1.0j) + white(seed + 1, n)
    return ya, yb


@pytest.mark.parametrize("amp_db, signal_bw", [(-12.0, None), (-12.0, 2000.0), (0.0, None), (10.0, 2000.0)])
def test_cw_returns_fdoa_without_tdoa(amp_db: float, signal_bw: float | None) -> None:
    n, fdoa = 31250, 21.7
    ya, yb = _cw_pair(41, n, amp_db, 40.0 + fdoa, 40.0)
    r = caf_estimate(ya, yb, FS, delay_window=(-3e-3, 37e-3), doppler_window=(-200.0, 200.0), signal_bw_hz=signal_bw)
    assert r.detected and r.tdoa_s is None and r.sigma_tdoa_s is None
    assert r.reason == caf_mod.REASON_NARROWBAND
    assert r.fdoa_hz is not None and r.sigma_fdoa_hz is not None
    # Frequency-difference bound for two tones in white noise (Rife and Boorstyn, one
    # tone: var f = 6 / ((2 pi)^2 snr N^3) fs^2), the two channels' errors adding.
    # The correlator adds the noise-times-noise term: a factor 1 + 1 / (2 g) in
    # variance, g the SNR in the correlation bandwidth.
    snr = 10.0 ** (amp_db / 10.0)
    sigma_tone = math.sqrt(2.0 * 6.0 / ((2.0 * math.pi) ** 2 * snr * float(n) ** 3)) * FS
    g = snr * FS / r.noise_bandwidth_hz
    expected = sigma_tone * math.sqrt(1.0 + 0.5 / g)
    assert abs(r.fdoa_hz - fdoa) < 5.0 * expected
    assert r.sigma_fdoa_hz == pytest.approx(expected, rel=0.3)
    assert len(r.candidates) == 1  # the ridge along delay is one peak, not many


def test_weak_cw_never_reports_a_delay() -> None:
    """Near threshold the ridge of a CW fluctuates along delay; no cell of it may pass as a delay peak.

    -16 dB per sample over 16384 samples is 9.9 dB post-correlation at any one lag, so
    a detection is the strongest of some 640 ridge cells lifted by noise. The FDOA
    sigma must follow the ridge (Stein bound at 9.9 dB), not the lifted cell.
    """
    n, fdoa, snr = 16384, 21.7, 10.0 ** (-1.6)
    stein_f = crlb.stein_sigmas(FS, 1.0, n / FS, snr, snr)[1]
    z, detected = [], 0
    for seed in range(24):
        ya, yb = _cw_pair(300 + 2 * seed, n, -16.0, 40.0 + fdoa, 40.0)
        r = caf_estimate(ya, yb, FS, delay_window=(-3e-3, 17e-3), doppler_window=(-100.0, 100.0))
        assert r.tdoa_s is None and r.sigma_tdoa_s is None
        if r.detected:
            detected += 1
            assert r.reason == caf_mod.REASON_NARROWBAND and r.rms_bandwidth_hz is None
            assert len(r.candidates) == 1
            assert r.sigma_fdoa_hz == pytest.approx(stein_f, rel=0.35)
            z.append((r.fdoa_hz - fdoa) / stein_f)
        else:
            assert r.reason == caf_mod.REASON_BELOW_THRESHOLD and r.fdoa_hz is None
    assert detected >= 12
    assert max(abs(v) for v in z) < 4.0 and 0.4 < float(np.std(z)) < 1.4


def test_strong_cw_ridge_is_found_wherever_its_strongest_cell_lies() -> None:
    """A ridge runs through the guard band as well as the window; it is one in-window detection."""
    n, fdoa = 16384, 21.7
    for seed in range(12):
        ya, yb = _cw_pair(700 + 2 * seed, n, -6.0, 40.0 + fdoa, 40.0)
        r = caf_estimate(ya, yb, FS, delay_window=(-1e-3, 1e-3), doppler_window=(-100.0, 100.0))
        assert r.detected and r.reason == caf_mod.REASON_NARROWBAND and r.tdoa_s is None
        assert abs(r.fdoa_hz - fdoa) < 5.0 * r.sigma_fdoa_hz
        assert -1e-3 - 1.0 / FS <= r.candidates[0][0] <= 1e-3 + 1.0 / FS  # reported inside the window
        # 28.2 dB at one lag: N C with C = (g / (1 + g))^2.
        assert r.snr_db == pytest.approx(10.0 * math.log10(n * (0.2512 / 1.2512) ** 2), abs=1.5)


def test_tdoa_withheld_when_sigma_exceeds_limit() -> None:
    """A 300 Hz signal: delay observable in principle, but coarser than max_tdoa_sigma_s at this SNR."""
    n, bw, tdoa, fdoa = 31250, 300.0, 4.0e-3, 5.5
    ya, yb = noise_pair(51, n, tdoa, fdoa, -22.0, -22.0, bw=bw)
    kw = dict(delay_window=(-20e-3, 20e-3), doppler_window=(-30.0, 30.0), signal_bw_hz=bw)
    r = caf_estimate(ya, yb, FS, **kw)
    assert r.detected and r.fdoa_hz is not None and abs(r.fdoa_hz - fdoa) < 5.0 * r.sigma_fdoa_hz
    g = 10.0 ** (-2.2) * FS / r.noise_bandwidth_hz
    s_t, _ = crlb.stein_sigmas(r.noise_bandwidth_hz, bw / math.sqrt(12.0), r.integration_s, g, g)
    assert 40e-6 < s_t < 400e-6
    assert r.sigma_tdoa_s == pytest.approx(s_t, rel=0.4)
    assert r.tdoa_s is not None and abs(r.tdoa_s - tdoa) < 5.0 * s_t
    tight = caf_estimate(ya, yb, FS, max_tdoa_sigma_s=0.5 * r.sigma_tdoa_s, **kw)
    assert tight.tdoa_s is None and tight.reason == caf_mod.REASON_NARROWBAND
    assert tight.fdoa_hz == pytest.approx(r.fdoa_hz) and tight.sigma_tdoa_s == pytest.approx(r.sigma_tdoa_s)


# ------------------------------------------------------------- several peaks, notches


def _two_copies(seed: int, n: int, amp_1: float, amp_2: float, p1: tuple[float, float], p2: tuple[float, float]):
    """Channel A hears the emitter twice (TDOA / FDOA p1 and p2 against channel B)."""
    src = Source(seed, n, FS, BW, pad_s=0.06)
    ya = amp_1 * src.path(D_B + p1[0], F_B + p1[1]) + amp_2 * src.path(D_B + p2[0], F_B + p2[1]) + white(seed + 1, n)
    yb = src.path(D_B, F_B) + white(seed + 2, n)
    return ya, yb


TRUE = (17.3e-3, -63.0)
SELF = (4.1e-3, 0.8)


@pytest.mark.parametrize("self_db", [6.0, 20.0])
def test_exclusion_removes_stronger_self_peak(self_db: float) -> None:
    amp_true = 0.2
    ya, yb = _two_copies(61, 31250, amp_true, amp_true * 10.0 ** (self_db / 20.0), TRUE, SELF)
    blind = caf_estimate(ya, yb, FS, **WIDE)
    assert blind.tdoa_s == pytest.approx(SELF[0], abs=2e-6) and blind.fdoa_hz == pytest.approx(SELF[1], abs=0.1)
    r = caf_estimate(ya, yb, FS, exclude=[SELF], **WIDE)
    assert r.detected and r.reason is None
    assert r.tdoa_s is not None and abs(r.tdoa_s - TRUE[0]) < 5.0 * r.sigma_tdoa_s
    assert abs(r.fdoa_hz - TRUE[1]) < 5.0 * r.sigma_fdoa_hz
    assert r.snr_db < blind.snr_db - 0.8 * self_db
    # Neither the self-peak nor its sidelobes are offered as candidates.
    assert all(abs(c[0] - SELF[0]) > 0.2e-3 or abs(c[1] - SELF[1]) > 150.0 for c in r.candidates)
    assert r.candidates[0] == pytest.approx((r.tdoa_s, r.fdoa_hz, r.snr_db))


def test_exclusion_of_a_cw_self_peak_notches_every_delay() -> None:
    """A tone common to both channels is a ridge along delay: its whole Doppler column is notched."""
    n = 31250
    t = np.arange(n) / FS
    ya, yb = noise_pair(71, n, TRUE[0], TRUE[1], -3.0, -3.0)
    ya = ya + 3.0 * np.exp(2j * np.pi * (500.0 + 30.0) * t)
    yb = yb + 3.0 * np.exp(2j * np.pi * 500.0 * t)
    blind = caf_estimate(ya, yb, FS, **WIDE)
    assert blind.fdoa_hz == pytest.approx(30.0, abs=0.05) and blind.tdoa_s is None
    r = caf_estimate(ya, yb, FS, exclude=[(10e-3, 30.0)], **WIDE)  # the delay of a ridge is arbitrary
    assert r.reason is None and r.tdoa_s is not None
    assert abs(r.tdoa_s - TRUE[0]) < 5.0 * r.sigma_tdoa_s and abs(r.fdoa_hz - TRUE[1]) < 5.0 * r.sigma_fdoa_hz
    assert all(abs(c[1] - 30.0) > 5.0 for c in r.candidates)


def test_exclusion_alone_in_window_gives_no_detection() -> None:
    ya, yb = noise_pair(81, 16384, SELF[0], SELF[1], 0.0, 0.0)
    kw = dict(delay_window=(-3e-3, 17e-3), doppler_window=(-100.0, 100.0), signal_bw_hz=BW)
    assert caf_estimate(ya, yb, FS, **kw).tdoa_s == pytest.approx(SELF[0], abs=1e-6)
    r = caf_estimate(ya, yb, FS, exclude=[SELF], **kw)
    assert not r.detected and r.tdoa_s is None and r.fdoa_hz is None
    assert r.reason == caf_mod.REASON_BELOW_THRESHOLD and r.snr_db < 13.0


def test_two_equal_peaks_are_ambiguous() -> None:
    other = (9.0e-3, 31.0)
    ya, yb = _two_copies(91, 31250, 0.5, 0.5, TRUE, other)
    r = caf_estimate(ya, yb, FS, **WIDE)
    assert r.detected and r.tdoa_s is None and r.fdoa_hz is None
    assert r.reason == caf_mod.REASON_AMBIGUOUS
    assert len(r.candidates) == 2
    found = sorted(r.candidates)
    for got, want in zip(found, sorted([TRUE, other])):
        assert got[0] == pytest.approx(want[0], abs=2e-6) and got[1] == pytest.approx(want[1], abs=0.05)
    assert abs(found[0][2] - found[1][2]) < 3.0 and min(c[2] for c in found) > 25.0
    # A window holding only one of them is unambiguous, and so is a 5 dB weaker second copy.
    one = caf_estimate(ya, yb, FS, delay_window=(12e-3, 22e-3), doppler_window=(-200.0, 200.0), signal_bw_hz=BW)
    assert one.reason is None and one.tdoa_s == pytest.approx(TRUE[0], abs=2e-6)
    ya2, yb2 = _two_copies(91, 31250, 0.5, 0.5 * 10.0 ** (-5.0 / 20.0), TRUE, other)
    weaker = caf_estimate(ya2, yb2, FS, **WIDE)
    assert weaker.reason is None and weaker.tdoa_s == pytest.approx(TRUE[0], abs=2e-6)
    assert len(weaker.candidates) == 2 and weaker.candidates[1][0] == pytest.approx(other[0], abs=5e-6)
    assert weaker.candidates[0][2] - weaker.candidates[1][2] == pytest.approx(5.0, abs=1.0)


def test_sawtooth_chirp_is_ambiguous() -> None:
    """25 kHz sweep every 10 ms: equal peaks every 10 ms of delay and at (40 us, 100 Hz) steps."""
    n = 31250
    t = np.arange(n) / FS
    tdoa, fdoa = TRUE
    ya = chirp(t - D_B - tdoa + 0.3, BW, 0.01) * np.exp(2j * np.pi * fdoa * t) + white(101, n)
    yb = chirp(t - D_B + 0.3, BW, 0.01) + white(102, n)
    r = caf_estimate(ya, yb, FS, **WIDE)
    assert r.detected and r.tdoa_s is None and r.fdoa_hz is None and r.reason == caf_mod.REASON_AMBIGUOUS
    assert len(r.candidates) == 8
    top = max(c[2] for c in r.candidates)
    for c in r.candidates:
        # Every candidate is a true ambiguity: a multiple of (40 us, 100 Hz) plus a multiple of 10 ms.
        k = round((c[1] - fdoa) / 100.0)
        assert abs(c[1] - fdoa - 100.0 * k) < 0.1
        resid = (c[0] - tdoa - k * 40e-6) / 0.01
        assert abs(resid - round(resid)) * 0.01 < 2e-6
        assert c[2] > top - 3.0
    # A window that holds the true peak alone gives a clean measurement.
    one = caf_estimate(ya, yb, FS, delay_window=(17.0e-3, 17.6e-3), doppler_window=(-90.0, -40.0), signal_bw_hz=BW)
    assert one.reason is None and abs(one.rho_tf) < 0.1
    assert abs(one.tdoa_s - tdoa) < 5.0 * one.sigma_tdoa_s + 20e-9 and abs(one.fdoa_hz - fdoa) < 5.0 * one.sigma_fdoa_hz


@pytest.mark.parametrize("sweeps, sign", [(2, 1.0), (2, -1.0), (4, 1.0)])
def test_rho_tf_of_repeated_sweeps(sweeps: int, sign: float) -> None:
    """m linear sweeps across the capture: time-frequency correlation 1 / m, sign of the sweep.

    rho = cov(t, f) / (sigma_t sigma_f) with t uniform over T and f a sawtooth of m
    periods: cov = k (T/m)^2 / 12, sigma_t = T / sqrt(12), sigma_f = k (T/m) / sqrt(12).
    """
    n = 8192
    t = np.arange(n) / FS
    period = n / FS / sweeps
    bw = sign * 20e3
    tdoa, fdoa = 12.0 / FS, 3.0
    ya = chirp(t - tdoa, bw, period) * np.exp(2j * np.pi * fdoa * t) + white(111, n, 0.5)
    yb = chirp(t, bw, period) + white(112, n, 0.5)
    step_hz = 1.0 / period  # neighbouring ambiguity peaks sit one sweep rate away in Doppler
    r = caf_estimate(
        ya, yb, FS, delay_window=(tdoa - 20e-6, tdoa + 20e-6), doppler_window=(fdoa - 0.4 * step_hz, fdoa + 0.4 * step_hz)
    )
    assert r.detected and r.reason is None
    assert r.rho_tf == pytest.approx(sign / sweeps, abs=0.06)
    assert abs(r.tdoa_s - tdoa) < 5.0 * r.sigma_tdoa_s + 30e-9 and abs(r.fdoa_hz - fdoa) < 5.0 * r.sigma_fdoa_hz


def test_single_sweep_is_delay_doppler_coupled() -> None:
    n = 8192
    t = np.arange(n) / FS
    tdoa, fdoa = 12.0 / FS, 3.0
    ya = chirp(t - tdoa + 0.05, 20e3, 0.4) * np.exp(2j * np.pi * fdoa * t) + white(121, n, 0.5)
    yb = chirp(t + 0.05, 20e3, 0.4) + white(122, n, 0.5)  # 0.05 .. 0.31 s of a 0.4 s sweep: no fly-back
    r = caf_estimate(ya, yb, FS, delay_window=(-1e-3, 2e-3), doppler_window=(-80.0, 80.0))
    assert r.detected and r.tdoa_s is None and r.fdoa_hz is None
    assert r.reason == caf_mod.REASON_COUPLED and r.rho_tf > 0.9


def test_mirror_window_flags_a_comparable_second_copy() -> None:
    mirror_pt = (9.0e-3, 31.0)
    primary = dict(delay_window=(12e-3, 22e-3), doppler_window=(-200.0, 200.0), signal_bw_hz=BW)
    mirror = ((5e-3, 11e-3), (-100.0, 100.0))
    ya, yb = _two_copies(131, 31250, 0.5, 0.5 * 10.0 ** (-4.0 / 20.0), TRUE, mirror_pt)  # 4 dB down
    plain = caf_estimate(ya, yb, FS, **primary)
    assert plain.reason is None and not plain.mirror_ambiguous
    r = caf_estimate(ya, yb, FS, mirror_window=mirror, **primary)
    assert r.mirror_ambiguous and r.reason is None
    assert r.tdoa_s == pytest.approx(plain.tdoa_s, abs=1e-9) and r.fdoa_hz == pytest.approx(plain.fdoa_hz, abs=1e-6)
    assert any(abs(c[0] - mirror_pt[0]) < 2e-6 and abs(c[1] - mirror_pt[1]) < 0.05 for c in r.candidates)
    # 9 dB down: reported as a candidate but not flagged.
    ya, yb = _two_copies(131, 31250, 0.5, 0.5 * 10.0 ** (-9.0 / 20.0), TRUE, mirror_pt)
    weak = caf_estimate(ya, yb, FS, mirror_window=mirror, **primary)
    assert not weak.mirror_ambiguous and weak.tdoa_s == pytest.approx(TRUE[0], abs=2e-6)
    # A mirror window that only sees the primary peak again is not a second copy.
    ya, yb = noise_pair(132, 31250, TRUE[0], TRUE[1], 0.0, 0.0)
    same = caf_estimate(ya, yb, FS, mirror_window=((10e-3, 20e-3), (-150.0, 50.0)), **primary)
    assert not same.mirror_ambiguous and len(same.candidates) == 1
    # A mirror copy stronger than the primary peak is flagged as well.
    ya, yb = _two_copies(133, 31250, 0.3, 0.6, TRUE, mirror_pt)
    strong = caf_estimate(ya, yb, FS, mirror_window=mirror, **primary)
    assert strong.mirror_ambiguous and strong.tdoa_s == pytest.approx(TRUE[0], abs=3e-6)


def test_peak_outside_window_is_not_reported() -> None:
    ya, yb = noise_pair(141, 16384, 5.0e-3, 10.0, 0.0, 0.0)
    kw = dict(doppler_window=(-50.0, 50.0), signal_bw_hz=BW)
    inside = caf_estimate(ya, yb, FS, delay_window=(4.0e-3, 6.0e-3), **kw)
    assert inside.tdoa_s == pytest.approx(5.0e-3, abs=1e-6)
    # The window ends 1.6 samples short of the peak: its skirt is inside, the peak is not.
    skirt = caf_estimate(ya, yb, FS, delay_window=(3.0e-3, 5.0e-3 - 1.6 / FS), **kw)
    assert not skirt.detected and skirt.tdoa_s is None and skirt.fdoa_hz is None
    assert skirt.reason in (caf_mod.REASON_OUTSIDE, caf_mod.REASON_BELOW_THRESHOLD)
    far = caf_estimate(ya, yb, FS, delay_window=(-3.0e-3, 3.0e-3), **kw)
    assert not far.detected and far.reason == caf_mod.REASON_BELOW_THRESHOLD and far.snr_db < 13.0


# ------------------------------------------------------------------ outputs and inputs


def test_display_outputs() -> None:
    tdoa, fdoa = 17.3e-3, -63.0
    ya, yb = noise_pair(151, 31250, tdoa, fdoa, 0.0, 0.0)
    r = caf_estimate(ya, yb, FS, t0_offset_s=0.0, **WIDE)
    assert isinstance(r, CafResult)
    assert r.surface.dtype == np.float32 and r.surface.ndim == 2
    assert r.surface.shape == (r.delay_axis_s.size, r.doppler_axis_hz.size)
    assert r.surface.shape[0] <= 65 and r.surface.shape[1] <= 65
    assert np.all(np.isfinite(r.surface))
    i, j = np.unravel_index(int(np.argmax(r.surface)), r.surface.shape)
    assert abs(r.delay_axis_s[i] - tdoa) <= 1.0 / FS and abs(r.doppler_axis_hz[j] - fdoa) <= 0.5
    assert r.snr_db - 4.5 < float(r.surface.max()) <= r.snr_db + 0.5  # grid loss only
    assert np.all(np.diff(r.delay_axis_s) > 0) and np.all(np.diff(r.doppler_axis_hz) > 0)
    assert np.diff(r.delay_axis_s) == pytest.approx(1.0 / FS)
    d_axis, d_db = r.delay_cut
    n_axis, n_db = r.doppler_cut
    assert d_axis.shape == d_db.shape and n_axis.shape == n_db.shape
    assert d_axis[0] <= -3e-3 + 1.0 / FS and d_axis[-1] >= 37e-3 - 1.0 / FS  # the full windows
    assert n_axis[0] <= -199.5 and n_axis[-1] >= 199.5
    assert abs(d_axis[int(np.argmax(d_db))] - tdoa) <= 1.0 / FS
    assert abs(n_axis[int(np.argmax(n_db))] - fdoa) <= 0.5
    assert abs(float(np.median(d_db)) - 10.0 * math.log10(math.log(2.0))) < 1.0  # the floor reads 0 dB (mean)
    assert d_axis.size <= 4096 and n_axis.size <= 4096
    assert all(isinstance(v, float) for c in r.candidates for v in c)
    for v in (r.tdoa_s, r.fdoa_hz, r.sigma_tdoa_s, r.sigma_fdoa_hz, r.rho_tf, r.snr_db, r.coherence, r.rms_bandwidth_hz):
        assert isinstance(v, float) and math.isfinite(v)


def test_long_cuts_are_reduced_for_display() -> None:
    ya, yb = noise_pair(161, 32768, 1.0e-3, 500.0, 0.0, 0.0, fs=250e3, bw=200e3, pad_s=0.004)
    r = caf_estimate(ya, yb, 250e3, delay_window=(-12e-3, 14e-3), doppler_window=(-3000.0, 3000.0), signal_bw_hz=200e3)
    assert r.tdoa_s == pytest.approx(1.0e-3, abs=5.0 * r.sigma_tdoa_s)
    d_axis, d_db = r.delay_cut
    assert 3000 < d_axis.size <= 4096  # 6500 lags, reduced by taking the maximum of pairs
    assert d_axis[0] < -11.9e-3 and d_axis[-1] > 13.9e-3
    assert abs(d_axis[int(np.argmax(d_db))] - 1.0e-3) <= 2.0 / 250e3
    assert float(d_db.max()) > r.snr_db - 4.5


def test_scale_dtype_and_determinism() -> None:
    ya, yb = noise_pair(171, 8192, 1.3137e-3, 12.345, -3.0, -3.0)
    kw = dict(delay_window=(0.3e-3, 2.3e-3), doppler_window=(-40.0, 40.0), signal_bw_hz=BW)
    r = caf_estimate(ya, yb, FS, **kw)
    again = caf_estimate(ya, yb, FS, **kw)
    assert again.tdoa_s == r.tdoa_s and again.fdoa_hz == r.fdoa_hz and again.snr_db == r.snr_db
    assert np.array_equal(again.surface, r.surface)
    scaled = caf_estimate(3e4 * ya, 2e-5 * yb, FS, **kw)
    assert scaled.tdoa_s == pytest.approx(r.tdoa_s, abs=1e-12) and scaled.snr_db == pytest.approx(r.snr_db, abs=1e-6)
    single = caf_estimate(ya.astype(np.complex64), yb.astype(np.complex64), FS, **kw)
    assert single.tdoa_s == pytest.approx(r.tdoa_s, abs=0.02 * r.sigma_tdoa_s)
    assert single.fdoa_hz == pytest.approx(r.fdoa_hz, abs=0.02 * r.sigma_fdoa_hz)
    shorter = caf_estimate(ya, yb[:7000], FS, **kw)  # unequal lengths are allowed
    assert shorter.tdoa_s == pytest.approx(r.tdoa_s, abs=5.0 * shorter.sigma_tdoa_s)
    assert shorter.integration_s < r.integration_s


def test_degenerate_inputs_give_finite_results() -> None:
    n = 4096
    x = white(221, n)
    kw = dict(delay_window=(-1e-3, 1e-3), doppler_window=(-50.0, 50.0))
    # Identical channels: coherence 1, Stein sigma 0; the reported sigma is the
    # numerical accuracy of the fine search, never zero.
    same = caf_estimate(x, x, FS, **kw)
    assert same.reason is None and abs(same.tdoa_s) < 1e-12 and abs(same.fdoa_hz) < 1e-9
    assert same.coherence == pytest.approx(1.0, abs=1e-9)
    assert same.sigma_tdoa_s == pytest.approx(2e-4 / FS) and same.sigma_fdoa_hz == pytest.approx(2e-4 / (n / FS))
    assert same.snr_db == pytest.approx(10.0 * math.log10(n), abs=1.0)  # B T C with C = 1
    # A tone in one channel only: |chi| does not depend on the lag at all (every
    # Doppler column is a ridge of noise level). Not a detection, and a level is reported.
    t = np.arange(n) / FS
    tone = caf_estimate(np.exp(2j * np.pi * 100.0 * t), x, FS, **kw)
    assert not tone.detected and tone.reason == caf_mod.REASON_BELOW_THRESHOLD and -5.0 < tone.snr_db < 13.0
    # Noise-free tones: a ridge, FDOA only.
    two = caf_estimate(np.exp(2j * np.pi * 100.0 * t), np.exp(2j * np.pi * 80.0 * t), FS, **kw)
    assert two.tdoa_s is None and two.reason == caf_mod.REASON_NARROWBAND and two.fdoa_hz == pytest.approx(20.0, abs=1e-6)
    for r in (same, tone, two):
        assert np.all(np.isfinite(r.surface)) and np.all(np.isfinite(r.delay_cut[1])) and math.isfinite(r.snr_db)


def test_narrow_delay_window() -> None:
    """A window of a couple of samples: statistics come from lags outside it, the peak from inside."""
    tdoa, fdoa = 1.3137e-3, 12.345
    ya, yb = noise_pair(181, 8192, tdoa, fdoa, -6.0, -6.0)
    r = caf_estimate(ya, yb, FS, delay_window=(tdoa - 25e-6, tdoa + 30e-6), doppler_window=(10.0, 14.0), signal_bw_hz=BW)
    assert r.reason is None and abs(r.tdoa_s - tdoa) < 4.0 * r.sigma_tdoa_s and abs(r.fdoa_hz - fdoa) < 4.0 * r.sigma_fdoa_hz
    g = 10.0 ** (-0.6) * FS / r.noise_bandwidth_hz
    rho = crlb.post_correlation_snr(r.noise_bandwidth_hz, r.integration_s, g, g)
    assert r.snr_db == pytest.approx(10.0 * math.log10(rho), abs=0.7)
    assert r.delay_cut[0].size <= 5 and r.surface.shape[0] >= 60  # cut: the window; patch: the neighbourhood


def test_invalid_input_raises_value_error_naming_the_argument() -> None:
    ya, yb = white(1, 1024), white(2, 1024)
    ok = dict(delay_window=(-1e-3, 1e-3), doppler_window=(-50.0, 50.0))

    def bad(match: str, *args: object, **kw: object) -> None:
        with pytest.raises(ValueError, match=match):
            caf_estimate(*args, **{**ok, **kw})  # type: ignore[arg-type]

    bad("ya", ya.reshape(2, 512), yb, FS)
    bad("yb", ya, yb[:10], FS)
    bad("ya", np.where(np.arange(1024) == 5, np.nan, ya), yb, FS)
    bad("yb", ya, np.zeros(1024, dtype=complex), FS)
    bad("fs", ya, yb, 0.0)
    bad("fs", ya, yb, float("nan"))
    bad("delay_window", ya, yb, FS, delay_window=(1e-3, -1e-3))
    bad("delay_window", ya, yb, FS, delay_window=(0.0, float("inf")))
    bad("delay_window", ya, yb, FS, delay_window=(0.0, 20e-3))  # beyond half of a 33 ms capture
    bad("delay_window", ya, yb, FS, delay_window=(0.0, 1e-3), t0_offset_s=-30e-3)
    bad("doppler_window", ya, yb, FS, doppler_window=(5.0, -5.0))
    bad("doppler_window", ya, yb, FS, doppler_window=(-9000.0, 9000.0))  # wider than fs / 2
    bad("doppler_window", ya, yb, FS, doppler_window=(0.0,))
    bad("signal_bw_hz", ya, yb, FS, signal_bw_hz=0.0)
    bad("signal_bw_hz", ya, yb, FS, signal_bw_hz=-5.0)
    bad("t0_offset_s", ya, yb, FS, t0_offset_s=float("nan"))
    bad("exclude", ya, yb, FS, exclude=[(1.0,)])
    bad("exclude", ya, yb, FS, exclude=[(float("nan"), 0.0)])
    bad("mirror_window", ya, yb, FS, mirror_window=((0.0, 1e-3),))
    bad("mirror_window", ya, yb, FS, mirror_window=((1e-3, 0.0), (-5.0, 5.0)))
    bad("mirror_window", ya, yb, FS, mirror_window=((0.0, 20e-3), (-5.0, 5.0)))
    bad("min_snr_db", ya, yb, FS, min_snr_db=float("nan"))
    bad("max_tdoa_sigma_s", ya, yb, FS, max_tdoa_sigma_s=0.0)


def test_threshold_argument_controls_detection() -> None:
    ya, yb = noise_pair(191, 8192, 1.3137e-3, 12.345, -11.0, -11.0)
    kw = dict(delay_window=(0.3e-3, 2.3e-3), doppler_window=(-40.0, 40.0), signal_bw_hz=BW)
    r = caf_estimate(ya, yb, FS, **kw)
    assert r.detected and 14.0 < r.snr_db < 20.0
    high = caf_estimate(ya, yb, FS, min_snr_db=r.snr_db + 1.0, **kw)
    assert not high.detected and high.tdoa_s is None and high.reason == caf_mod.REASON_BELOW_THRESHOLD
    assert high.snr_db == pytest.approx(r.snr_db, abs=0.8)


# ------------------------------------------------------------------------ performance


def _best_time(fn, repeats: int = 2) -> float:
    best = float("inf")
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best


def test_speed_default_uhf_capture() -> None:
    """31250 samples, 40 ms by +/- 200 Hz: target 0.3 s (bound loose for a shared machine)."""
    ya, yb = noise_pair(201, 31250, 17.3e-3, -63.0, 0.0, 0.0)
    assert _best_time(lambda: caf_estimate(ya, yb, FS, **WIDE)) < 1.5


@pytest.mark.slow
def test_speed_long_capture() -> None:
    """262144 samples, 1 ms by +/- 5 kHz: target 2 s (bound loose for a shared machine)."""
    fs = 2e6
    ya, yb = noise_pair(211, 262144, 0.3123e-3, 1234.5, -5.0, -5.0, fs=fs, bw=1.6e6, pad_s=0.002)
    out: list[CafResult] = []
    elapsed = _best_time(
        lambda: out.append(
            caf_estimate(ya, yb, fs, delay_window=(-0.5e-3, 0.5e-3), doppler_window=(-5e3, 5e3), signal_bw_hz=1.6e6)
        )
    )
    r = out[-1]
    assert r.reason is None and abs(r.tdoa_s - 0.3123e-3) < 5.0 * r.sigma_tdoa_s
    assert abs(r.fdoa_hz - 1234.5) < 5.0 * r.sigma_fdoa_hz
    assert elapsed < 8.0
