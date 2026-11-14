"""Stein bounds (MATH.md section 8) against independent closed forms and quadrature."""
from __future__ import annotations

import math

import numpy as np
import pytest

from petal_locate.core import crlb


def test_effective_snr_and_coherence_identities() -> None:
    g1, g2 = 0.37, 4.2
    g = crlb.effective_snr(g1, g2)
    assert 1.0 / g == pytest.approx(0.5 * (1.0 / g1 + 1.0 / g2 + 1.0 / (g1 * g2)), rel=1e-14)
    c = crlb.coherence(g1, g2)
    assert c == pytest.approx(g1 * g2 / ((1.0 + g1) * (1.0 + g2)), rel=1e-14)
    # MATH.md section 8: B T g = 2 B T C / (1 - C).
    assert g == pytest.approx(2.0 * c / (1.0 - c), rel=1e-13)
    # Equal SNRs: g = 2 g1^2 / (1 + 2 g1); one clean channel: g -> 2 g1, C -> g1 / (1 + g1).
    assert crlb.effective_snr(0.5, 0.5) == pytest.approx(0.25)
    assert crlb.effective_snr(0.37, 1e15) == pytest.approx(0.74, rel=1e-9)
    assert crlb.coherence(0.37, 1e15) == pytest.approx(0.37 / 1.37, rel=1e-9)
    assert crlb.effective_snr(0.0, 3.0) == 0.0 and crlb.coherence(0.0, 3.0) == 0.0
    assert isinstance(g, float) and isinstance(c, float)


def test_post_correlation_snr_limits() -> None:
    bw, t = 25e3, 1.0
    assert crlb.post_correlation_snr(bw, t, 1.0, 1.0) == pytest.approx(bw * t / 4.0)
    # Low SNR: rho -> B T g / 2 = B T g1 g2.
    g1, g2 = 1e-4, 3e-4
    rho = crlb.post_correlation_snr(bw, t, g1, g2)
    assert rho == pytest.approx(0.5 * bw * t * crlb.effective_snr(g1, g2), rel=1e-3)
    assert rho == pytest.approx(bw * t * g1 * g2, rel=1e-3)
    # High SNR saturates at the number of independent samples B T.
    assert crlb.post_correlation_snr(bw, t, 1e9, 1e9) == pytest.approx(bw * t, rel=1e-6)


def test_stein_sigmas_reference_numbers() -> None:
    """25 kHz flat signal, 1 s, 0 dB in both channels: hand calculation."""
    bw, t, g1, g2 = 25e3, 1.0, 1.0, 1.0
    s_t, s_f = crlb.stein_sigmas(bw, bw / math.sqrt(12.0), t, g1, g2)
    g = 2.0 / 3.0
    beta = 2.0 * math.pi * 25e3 / math.sqrt(12.0)  # 45345 rad/s
    assert s_t == pytest.approx(1.0 / (beta * math.sqrt(bw * t * g)), rel=1e-12)
    assert s_t == pytest.approx(170.8e-9, rel=2e-3)
    assert s_f == pytest.approx(math.sqrt(3.0) / (math.pi * t * math.sqrt(bw * t * g)), rel=1e-12)
    assert s_f == pytest.approx(4.271e-3, rel=2e-3)


def test_fdoa_bound_equals_single_tone_frequency_bound() -> None:
    """With a noiseless reference the FDOA bound is the frequency CRLB of a tone in white noise.

    Rife and Boorstyn: var(omega) = 6 / (snr N (N^2 - 1)) rad^2 per sample^2 for a
    complex tone of per-sample SNR `snr` over N samples.
    """
    fs, n, snr = 31250.0, 31250, 0.2
    var_f = 6.0 / (snr * n * (n * n - 1.0)) * (fs / (2.0 * math.pi)) ** 2
    _, s_f = crlb.stein_sigmas(fs, fs / math.sqrt(12.0), n / fs, snr, 1e15)
    assert s_f == pytest.approx(math.sqrt(var_f), rel=1e-6)


def test_tdoa_bound_equals_known_signal_delay_bound() -> None:
    """With a noiseless reference the TDOA bound is the delay CRLB of a known signal.

    var(tau) = 1 / (2 (E / N0) beta^2), E / N0 = g1 B T for a flat signal of width B.
    """
    bw, t, g1 = 40e3, 0.5, 0.05
    beta = 2.0 * math.pi * bw / math.sqrt(12.0)
    s_t, _ = crlb.stein_sigmas(bw, bw / math.sqrt(12.0), t, g1, 1e15)
    assert s_t == pytest.approx(1.0 / (beta * math.sqrt(2.0 * g1 * bw * t)), rel=1e-6)


def _plain_correlator_sigmas(bw: float, sig_bw: float, t: float, g1: float, g2: float) -> tuple[float, float]:
    """Variance of the plain cross-correlator by quadrature over frequency.

    For generalised cross-correlation with unit weighting (complex baseband, spectra
    two-sided over B):
        var(tau) = int w^2 (G11 G22 - |G12|^2) df / (2 T (int w^2 |G12| df)^2)
        var(nu)  = int     (G11 G22 - |G12|^2) df / (2 T (int     |G12| df)^2) / T_e'^2
    with w = 2 pi f about the signal centroid and T_e'^2 = (2 pi)^2 T^2 / 12.
    Signal: flat over sig_bw (power g_i in units of that channel's noise power in B).
    """
    f = np.linspace(-bw / 2.0, bw / 2.0, 400_001)
    df = f[1] - f[0]
    sig = (np.abs(f) <= sig_bw / 2.0).astype(float) / sig_bw  # unit power, per Hz
    n1 = np.full_like(f, 1.0 / (g1 * bw))
    n2 = np.full_like(f, 1.0 / (g2 * bw))
    g11, g22, g12 = sig + n1, sig + n2, sig
    w2 = (2.0 * math.pi * f) ** 2
    incoherent = g11 * g22 - g12 * g12
    var_t = np.sum(w2 * incoherent) * df / (2.0 * t * (np.sum(w2 * g12) * df) ** 2)
    var_f = np.sum(incoherent) * df / (2.0 * t * (np.sum(g12) * df) ** 2) / ((2.0 * math.pi * t) ** 2 / 12.0)
    return math.sqrt(var_t), math.sqrt(var_f)


@pytest.mark.parametrize("fill", [1.0, 0.8, 0.32])
@pytest.mark.parametrize("g1, g2", [(0.05, 0.05), (0.3, 3.0), (20.0, 8.0)])
def test_stein_sigmas_match_quadrature_of_the_correlator_variance(fill: float, g1: float, g2: float) -> None:
    bw, t = 31250.0, 0.8
    sig_bw = fill * bw
    s_t, s_f = crlb.stein_sigmas(bw, sig_bw / math.sqrt(12.0), t, g1, g2)
    q_t, q_f = _plain_correlator_sigmas(bw, sig_bw, t, g1, g2)
    assert s_t == pytest.approx(q_t, rel=2e-4)
    assert s_f == pytest.approx(q_f, rel=2e-4)  # sigma_fdoa does not depend on the fill


def test_q_term_of_a_signal_filling_part_of_the_band() -> None:
    bw, t = 31250.0, 1.0
    rms = 0.8 * bw / math.sqrt(12.0)

    def ratio(g: float) -> float:
        with_q = crlb.stein_sigmas(bw, rms, t, g, g)[0]
        plain = 1.0 / (2.0 * math.pi * rms * math.sqrt(bw * t * crlb.effective_snr(g, g)))
        return with_q / plain

    # q = 1 / 0.8^2 = 1.5625: the excess tends to sqrt(q) = 1.25 at low SNR and to 1 at high SNR.
    assert ratio(1e-6) == pytest.approx(1.25, rel=1e-4)
    assert ratio(1e6) == pytest.approx(1.0, rel=1e-4)
    # MATH.md section 8 quotes a measured 1.22 for this fill; the formula gives it at about -11 dB.
    assert ratio(0.076) == pytest.approx(1.22, abs=0.005)
    assert 1.0 < ratio(1.0) < ratio(0.1) < 1.25
    # No q below 1: a signal with more RMS bandwidth than a flat one over B is not rewarded.
    wide = crlb.stein_sigmas(bw, 1.3 * bw / math.sqrt(12.0), t, 0.1, 0.1)[0]
    assert wide == pytest.approx(
        1.0 / (2.0 * math.pi * 1.3 * bw / math.sqrt(12.0) * math.sqrt(bw * t * crlb.effective_snr(0.1, 0.1))), rel=1e-12
    )


def test_sigmas_from_coherence_equal_stein_when_signal_fills_band() -> None:
    bw, t = 26e3, 0.6
    rms = bw / math.sqrt(12.0)
    for g1, g2 in [(0.01, 0.02), (0.5, 0.5), (3.0, 40.0)]:
        want = crlb.stein_sigmas(bw, rms, t, g1, g2)
        got = crlb.sigmas_from_coherence(crlb.coherence(g1, g2), bw, rms, t)
        assert got[0] == pytest.approx(want[0], rel=1e-12)
        assert got[1] == pytest.approx(want[1], rel=1e-12)


def test_noise_rms_bandwidth_reproduces_the_q_term() -> None:
    """The data-driven form used by the estimator against the closed form for flat spectra.

    The incoherent cross-spectral power is S (N1 + N2) over the signal band plus N1 N2
    over all of B; its RMS bandwidth over that of the signal is the factor on sigma_tdoa.
    """
    bw, t = 31250.0, 0.5
    for fill in (1.0, 0.8, 0.32):
        sig_bw = fill * bw
        for g1, g2 in [(0.05, 0.05), (0.3, 3.0), (20.0, 8.0)]:
            s = 1.0 / sig_bw
            n1, n2 = 1.0 / (g1 * bw), 1.0 / (g2 * bw)
            in_sig = s * (n1 + n2) * sig_bw  # total incoherent power of each kind
            in_all = n1 * n2 * bw
            var_noise = (in_sig * sig_bw**2 / 12.0 + in_all * bw**2 / 12.0) / (in_sig + in_all)
            got = crlb.sigmas_from_coherence(
                crlb.coherence(g1, g2), bw, sig_bw / math.sqrt(12.0), t, noise_rms_bw_hz=math.sqrt(var_noise)
            )
            want = crlb.stein_sigmas(bw, sig_bw / math.sqrt(12.0), t, g1, g2)
            assert got[0] == pytest.approx(want[0], rel=1e-10)
            assert got[1] == pytest.approx(want[1], rel=1e-10)


def test_scaling_laws() -> None:
    base = crlb.stein_sigmas(25e3, 7000.0, 1.0, 0.1, 0.1)
    longer = crlb.stein_sigmas(25e3, 7000.0, 4.0, 0.1, 0.1)
    assert longer[0] == pytest.approx(base[0] / 2.0)  # sigma_tdoa ~ T^-1/2
    assert longer[1] == pytest.approx(base[1] / 8.0)  # sigma_fdoa ~ T^-3/2
    c = 0.01
    a = crlb.sigmas_from_coherence(c, 25e3, 7000.0, 1.0)
    b = crlb.sigmas_from_coherence(c, 25e3, 3500.0, 1.0)
    assert b[0] == pytest.approx(2.0 * a[0]) and b[1] == pytest.approx(a[1])


def test_broadcasting_and_degenerate_values() -> None:
    g = np.array([0.01, 0.1, 1.0, 10.0])
    s_t, s_f = crlb.stein_sigmas(25e3, 7217.0, 1.0, g, 1.0)
    assert s_t.shape == (4,) and s_f.shape == (4,)
    assert np.all(np.diff(s_t) < 0) and np.all(np.diff(s_f) < 0)
    for i, gi in enumerate(g):
        one = crlb.stein_sigmas(25e3, 7217.0, 1.0, float(gi), 1.0)
        assert s_t[i] == pytest.approx(one[0]) and s_f[i] == pytest.approx(one[1])
    rho = crlb.post_correlation_snr(np.array([1e3, 2e3]), 2.0, 1.0, 1.0)
    assert rho == pytest.approx([500.0, 1000.0])
    # A CW has no RMS bandwidth: delay unobservable, Doppler fine.
    s_t, s_f = crlb.stein_sigmas(25e3, 0.0, 1.0, 1.0, 1.0)
    assert math.isinf(s_t) and math.isfinite(s_f) and s_f > 0.0
    s_t, s_f = crlb.sigmas_from_coherence(0.2, 25e3, 0.0, 1.0)
    assert math.isinf(s_t) and math.isfinite(s_f)
    # No signal in one channel: nothing is observable. Perfect coherence: no noise.
    assert all(math.isinf(v) for v in crlb.stein_sigmas(25e3, 7217.0, 1.0, 0.0, 1.0))
    assert all(math.isinf(v) for v in crlb.sigmas_from_coherence(0.0, 25e3, 7217.0, 1.0))
    assert crlb.sigmas_from_coherence(1.0, 25e3, 7217.0, 1.0) == (0.0, 0.0)


@pytest.mark.parametrize(
    "call, name",
    [
        (lambda: crlb.effective_snr(-1.0, 1.0), "g1"),
        (lambda: crlb.effective_snr(1.0, float("nan")), "g2"),
        (lambda: crlb.coherence(1.0, -0.1), "g2"),
        (lambda: crlb.post_correlation_snr(0.0, 1.0, 1.0, 1.0), "noise_bw_hz"),
        (lambda: crlb.post_correlation_snr(1e3, -1.0, 1.0, 1.0), "T_s"),
        (lambda: crlb.stein_sigmas(1e3, -5.0, 1.0, 1.0, 1.0), "rms_bw_hz"),
        (lambda: crlb.stein_sigmas(float("inf"), 5.0, 1.0, 1.0, 1.0), "noise_bw_hz"),
        (lambda: crlb.stein_sigmas(1e3, 5.0, 0.0, 1.0, 1.0), "T_s"),
        (lambda: crlb.stein_sigmas(1e3, 5.0, 1.0, np.array([1.0, -2.0]), 1.0), "g1"),
        (lambda: crlb.sigmas_from_coherence(1.5, 1e3, 5.0, 1.0), "C"),
        (lambda: crlb.sigmas_from_coherence(-0.1, 1e3, 5.0, 1.0), "C"),
        (lambda: crlb.sigmas_from_coherence(0.5, 1e3, 5.0, 1.0, noise_rms_bw_hz=-1.0), "noise_rms_bw_hz"),
    ],
)
def test_invalid_input_raises_value_error_naming_the_argument(call, name: str) -> None:
    with pytest.raises(ValueError, match=name):
        call()
