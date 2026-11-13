"""Reported sigmas of `caf_estimate` with co-channel copies, its per-channel SNRs and the
noise shares that rows of one capture have in common (MATH.md sections 6 and 8).

Synthetic captures: a band-limited complex Gaussian signal delayed differently in each
channel, white noise, and (co-channel case) a weaker copy of the other channel's path in
each channel, all at one site so that the two self-peaks coincide.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from petal_locate.core.caf import caf_estimate

FS = 20_000.0
N = 20_000
BW = 10_000.0


def band_noise(rng: np.random.Generator, n: int) -> np.ndarray:
    """Unit-power complex Gaussian noise limited to +/- BW / 2 (FFT mask)."""
    x = (rng.standard_normal(n) + 1j * rng.standard_normal(n)) / math.sqrt(2.0)
    f = np.fft.fftfreq(n, 1.0 / FS)
    spec = np.fft.fft(x)
    spec[np.abs(f) > 0.5 * BW] = 0.0
    out = np.fft.ifft(spec)
    return out / math.sqrt(np.mean(np.abs(out) ** 2))


def white(rng: np.random.Generator, n: int, power: float) -> np.ndarray:
    return (rng.standard_normal(n) + 1j * rng.standard_normal(n)) * math.sqrt(0.5 * power)


def delayed(s: np.ndarray, pad: int, d: int) -> np.ndarray:
    return s[pad - d: pad - d + N]


@pytest.mark.slow
def test_coinciding_self_peaks_sigma_matches_scatter() -> None:
    """A = s(t - d1) + e s(t - d2) + n, B = s(t - d2) + e s(t - d1) + n (one site, both
    satellites in both channels): the self-peaks of the two satellites coincide at zero
    lag and their sidelobes add in amplitude at the peak. Before the co-channel term the
    reported TDOA sigma was about 1.3 times too small here (and 1.08 / 1.11 for TDOA /
    FDOA on the simulator's seoul-pac / seoul-io pair, K5); with it the scatter over
    the reported sigma is 1 within its standard error."""
    rng = np.random.default_rng(55)
    d1, d2, leak = 40, -20, 0.5
    pad = 100
    e_t, e_f, excess = [], [], []
    for _ in range(80):
        s = band_noise(rng, N + 2 * pad)
        ya = delayed(s, pad, d1) + leak * delayed(s, pad, d2) + white(rng, N, 0.5)
        yb = delayed(s, pad, d2) + leak * delayed(s, pad, d1) + white(rng, N, 0.5)
        true_tdoa = (d1 - d2) / FS
        r = caf_estimate(ya, yb, FS, delay_window=(1e-3, 5e-3), doppler_window=(-20.0, 20.0), signal_bw_hz=BW,
                         exclude=[(0.0, 0.0), (0.0, 0.0)], mirror_window=((-5e-3, -1e-3), (-20.0, 20.0)))
        assert r.tdoa_s is not None and not r.mirror_ambiguous, r.reason
        e_t.append((r.tdoa_s - true_tdoa) / r.sigma_tdoa_s)
        e_f.append(r.fdoa_hz / r.sigma_fdoa_hz)
        excess.append(r.leak_excess)
    se = 1.0 / math.sqrt(2.0 * (len(e_t) - 1))
    # the term itself: 2 sqrt(C_1 C_2) / C_0 = 2 * leak^2 with equal paths
    assert np.median(excess) == pytest.approx(2.0 * leak**2, rel=0.15)
    assert abs(np.std(e_t, ddof=1) - 1.0) < 3.0 * se
    assert abs(np.std(e_f, ddof=1) - 1.0) < 3.0 * se


def test_no_coinciding_self_peaks_no_term() -> None:
    """Self-peaks apart (different sites), a single one, or none: nothing is added."""
    rng = np.random.default_rng(3)
    pad = 100
    s = band_noise(rng, N + 2 * pad)
    ya = delayed(s, pad, 40) + 0.5 * delayed(s, pad, -20) + white(rng, N, 0.5)
    yb = delayed(s, pad, -20) + 0.5 * delayed(s, pad, 10) + white(rng, N, 0.5)
    for exclude in ([], [(0.0, 0.0)], [(0.0, 0.0), (1.5e-3, 0.0)]):
        r = caf_estimate(ya, yb, FS, delay_window=(2e-3, 4e-3), doppler_window=(-20.0, 20.0), signal_bw_hz=BW,
                         exclude=exclude)
        assert r.tdoa_s is not None, r.reason
        assert r.leak_excess == 0.0


def test_per_channel_snrs_and_noise_shares() -> None:
    """Different SNRs in the two channels, the signal in half the capture band: the noise
    floor outside the band gives each channel's SNR in B, and the shares are
    ``(1/g) / (1/g_a + 1/g_b + q/(g_a g_b))`` of the reported variances (q from the
    estimator's bandwidths for TDOA, 1 for FDOA), summing to less than 1."""
    rng = np.random.default_rng(11)
    pad = 100
    g_a_true, g_b_true = 10.0, 2.0  # in the signal band
    s = band_noise(rng, N + 2 * pad)
    # white noise of density n0 over fs: in the signal band BW its power is n0 * BW
    ya = math.sqrt(g_a_true) * delayed(s, pad, 30) + white(rng, N, FS / BW)
    yb = math.sqrt(g_b_true) * delayed(s, pad, 0) + white(rng, N, FS / BW)
    r = caf_estimate(ya, yb, FS, delay_window=(0.5e-3, 3e-3), doppler_window=(-20.0, 20.0), signal_bw_hz=BW)
    assert r.tdoa_s is not None and r.snr_measured
    band = r.noise_bandwidth_hz / BW  # the filter's noise band is a little wider than the signal
    assert r.snr_a_db == pytest.approx(10 * math.log10(g_a_true / band), abs=0.5)
    assert r.snr_b_db == pytest.approx(10 * math.log10(g_b_true / band), abs=0.5)
    ga, gb = 10 ** (r.snr_a_db / 10), 10 ** (r.snr_b_db / 10)
    fdoa_a, fdoa_b = r.noise_share_fdoa
    assert fdoa_a == pytest.approx((1 / ga) / (1 / ga + 1 / gb + 1 / (ga * gb)), rel=0.05)
    assert fdoa_b == pytest.approx((1 / gb) / (1 / ga + 1 / gb + 1 / (ga * gb)), rel=0.05)
    assert fdoa_b > fdoa_a  # the weaker channel's noise dominates
    tdoa_a, tdoa_b = r.noise_share_tdoa
    assert 0.0 < tdoa_a < fdoa_a + 1e-9 and 0.0 < tdoa_b <= fdoa_b + 1e-9 and tdoa_a + tdoa_b < 1.0


def test_snr_without_room_outside_the_band() -> None:
    """A signal filling the capture: no noise floor can be read, equal SNRs consistent with
    the coherence are reported (``snr_measured`` False)."""
    rng = np.random.default_rng(12)
    x = (rng.standard_normal(N + 200) + 1j * rng.standard_normal(N + 200)) / math.sqrt(2.0)
    ya = x[100 - 30: 100 - 30 + N] + white(rng, N, 0.3)
    yb = x[100: 100 + N] + white(rng, N, 0.3)
    r = caf_estimate(ya, yb, FS, delay_window=(0.5e-3, 3e-3), doppler_window=(-20.0, 20.0), signal_bw_hz=None)
    assert r.tdoa_s is not None and not r.snr_measured
    assert r.snr_a_db == pytest.approx(r.snr_b_db)
    g = 10 ** (r.snr_a_db / 10)
    assert (g / (1 + g)) ** 2 == pytest.approx(r.coherence, rel=1e-6)


@pytest.mark.slow
def test_pairs_sharing_a_capture_correlate_as_the_shares_say() -> None:
    """Pairs (A, B) and (A, C) correlated from one capture of A: their TDOA (FDOA) errors
    correlate by ``sqrt(share_a,AB share_a,AC)``, A's noise share in each (MATH-2 review:
    0.48 measured at equal SNRs, 0.79 with A 6 dB weaker)."""
    rng = np.random.default_rng(21)
    pad = 100
    errs, predicted = [], []
    for _ in range(150):
        s = band_noise(rng, N + 2 * pad)
        # in the signal band: A at 6 dB, B and C at 12 dB
        ya = 2.0 * delayed(s, pad, 30) + white(rng, N, FS / BW)
        yb = 4.0 * delayed(s, pad, 0) + white(rng, N, FS / BW)
        yc = 4.0 * delayed(s, pad, -25) + white(rng, N, FS / BW)
        r_ab = caf_estimate(ya, yb, FS, delay_window=(0.5e-3, 3e-3), doppler_window=(-20.0, 20.0), signal_bw_hz=BW)
        r_ac = caf_estimate(ya, yc, FS, delay_window=(1.5e-3, 4.5e-3), doppler_window=(-20.0, 20.0), signal_bw_hz=BW)
        assert r_ab.tdoa_s is not None and r_ac.tdoa_s is not None
        errs.append((r_ab.tdoa_s - 30 / FS, r_ac.tdoa_s - 55 / FS, r_ab.fdoa_hz, r_ac.fdoa_hz))
        predicted.append((math.sqrt(r_ab.noise_share_tdoa[0] * r_ac.noise_share_tdoa[0]),
                          math.sqrt(r_ab.noise_share_fdoa[0] * r_ac.noise_share_fdoa[0])))
    e = np.array(errs)
    corr_t = np.corrcoef(e[:, 0], e[:, 1])[0, 1]
    corr_f = np.corrcoef(e[:, 2], e[:, 3])[0, 1]
    pred_t, pred_f = np.mean(predicted, axis=0)
    se = (1 - pred_t**2) / math.sqrt(len(errs))
    assert pred_t > 0.6  # A is the weak channel: its noise dominates both pairs
    assert corr_t == pytest.approx(pred_t, abs=3 * se + 0.03)
    assert corr_f == pytest.approx(pred_f, abs=3 * se + 0.03)
