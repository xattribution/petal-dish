"""Stein bounds for TDOA / FDOA estimated by cross-correlation (MATH.md section 8).

Conventions shared by every function here:

- ``noise_bw_hz`` is ``B``: the bandwidth the two channels are correlated over, in Hz
  (the noise bandwidth of the band filter of MATH.md section 6, or the capture rate
  when no filter is applied).
- ``T_s`` is the integration time in seconds (samples in the correlation sum / fs).
- ``g1``, ``g2`` are the linear (not dB) input signal-to-noise ratios of the two
  channels measured in ``B``.
- ``rms_bw_hz`` is the RMS bandwidth ``B_rms`` of the signal in Hz
  (``B_s / sqrt(12)`` for a flat spectrum of width ``B_s``); ``beta = 2 pi B_rms``.
- ``rho_tf`` is the time-frequency correlation of the signal (-1 < rho_tf < 1): the
  correlation of delay and Doppler in the curvature of ``|chi|^2`` at the peak, the
  quantity `caf_estimate` reports (0 for noise-like signals, 1 / m for m linear sweeps
  across the capture).
- All functions broadcast over numpy arrays and return Python floats for scalar input.

Delay-Doppler coupling. The peak of the cross-ambiguity function errs by
``M^-1 g``: ``M`` the curvature of the peak, proportional to
``[[beta^2, -rho beta T_e], [-rho beta T_e, T_e^2]]``, and ``g`` the gradient of the
noise on the surface. The signal-times-noise part of ``g`` has covariance
proportional to ``M`` itself; the noise-times-noise part has none of the signal's
coupling, its covariance is ``diag(q beta^2, T_e^2)``. With ``a = 1/g1 + 1/g2`` and
``b = 1/(g1 g2)`` the error covariance ``M^-1 (a M + b diag(q beta^2, T_e^2)) M^-1 / (2 B T)``
gives, with ``k = 1 / (1 - rho^2)``::

    var(tau) = [ (a + q b) k + rho^2 (1 + q) b k^2 ] / (2 B T beta^2)
    var(nu)  = [ (a +   b) k + rho^2 (1 + q) b k^2 ] / (2 B T T_e^2)

MATH.md section 6 item 4 (multiply both sigmas by ``sqrt(k)``) is the case b = 0: one
channel free of noise. With noise in both channels it understates the scatter, by
1.25 in FDOA at rho = 0.5 and equal 0 dB input SNRs, and by 3 near rho = 0.9
(measured, tests/test_caf_verify.py).
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

__all__ = [
    "effective_snr",
    "coherence",
    "post_correlation_snr",
    "stein_sigmas",
    "sigmas_from_coherence",
]


def _out(x: np.ndarray) -> Any:
    """Return a Python float for 0-d results, the array otherwise."""
    return float(x) if np.ndim(x) == 0 else x


def _nonneg(value: Any, name: str) -> np.ndarray:
    arr = np.asarray(value, dtype=np.float64)
    if not np.all(np.isfinite(arr)) or np.any(arr < 0.0):
        raise ValueError(f"{name} must be finite and >= 0 (got {value!r})")
    return arr


def _positive(value: Any, name: str) -> np.ndarray:
    arr = np.asarray(value, dtype=np.float64)
    if not np.all(np.isfinite(arr)) or np.any(arr <= 0.0):
        raise ValueError(f"{name} must be finite and > 0 (got {value!r})")
    return arr


def _inverse_sum(g1: np.ndarray, g2: np.ndarray, q: Any = 1.0) -> np.ndarray:
    """1/g1 + 1/g2 + q/(g1 g2); +inf where either SNR is zero."""
    with np.errstate(divide="ignore", invalid="ignore"):
        s = 1.0 / g1 + 1.0 / g2 + np.asarray(q, dtype=np.float64) / (g1 * g2)
    return np.where((g1 > 0.0) & (g2 > 0.0), s, np.inf)


def effective_snr(g1: Any, g2: Any) -> Any:
    """Effective input SNR ``g`` of a correlated pair (linear, dimensionless).

    ``1/g = 0.5 (1/g1 + 1/g2 + 1/(g1 g2))``, i.e. ``g = 2 g1 g2 / (1 + g1 + g2)``.
    g1, g2: linear input SNRs in the correlation bandwidth, >= 0.
    """
    a, b = _nonneg(g1, "g1"), _nonneg(g2, "g2")
    return _out(2.0 * a * b / (1.0 + a + b))


def coherence(g1: Any, g2: Any) -> Any:
    """Magnitude-squared coherence ``C = g1 g2 / ((1 + g1)(1 + g2))`` in ``B`` (0..1).

    g1, g2: linear input SNRs in the correlation bandwidth, >= 0.
    """
    a, b = _nonneg(g1, "g1"), _nonneg(g2, "g2")
    return _out(a * b / ((1.0 + a) * (1.0 + b)))


def post_correlation_snr(noise_bw_hz: Any, T_s: Any, g1: Any, g2: Any) -> Any:
    """Post-correlation SNR ``rho = B T C`` (linear power ratio; MATH.md section 8).

    This is the CAF peak power over the mean off-peak power of the surface, the
    quantity `caf_estimate` thresholds. It tends to ``B T g / 2`` for g1, g2 << 1.
    noise_bw_hz: B in Hz (> 0). T_s: integration time in s (> 0). g1, g2: linear SNRs in B.
    """
    bw, t = _positive(noise_bw_hz, "noise_bw_hz"), _positive(T_s, "T_s")
    a, b = _nonneg(g1, "g1"), _nonneg(g2, "g2")
    return _out(bw * t * (a * b / ((1.0 + a) * (1.0 + b))))


def _coupling(rho_tf: Any) -> tuple[np.ndarray, np.ndarray]:
    """(rho^2, k = 1 / (1 - rho^2)) for a time-frequency correlation -1 < rho_tf < 1."""
    rho = np.asarray(rho_tf, dtype=np.float64)
    if not np.all(np.isfinite(rho)) or np.any(np.abs(rho) >= 1.0):
        raise ValueError(f"rho_tf must be within (-1, 1) (got {rho_tf!r})")
    rho2 = rho * rho
    return rho2, 1.0 / (1.0 - rho2)


def stein_sigmas(
    noise_bw_hz: Any, rms_bw_hz: Any, T_s: Any, g1: Any, g2: Any, *, rho_tf: Any = 0.0
) -> tuple[Any, Any]:
    """One-sigma TDOA (s) and FDOA (Hz) of a cross-correlation estimate.

    ``sigma_tdoa = sqrt((1/g1 + 1/g2 + q/(g1 g2)) / (2 B T)) / beta`` with
    ``beta = 2 pi B_rms`` and ``q = max(1, B^2 / (12 B_rms^2))``;
    ``sigma_fdoa = sqrt((1/g1 + 1/g2 + 1/(g1 g2)) / (2 B T)) / T_e`` with
    ``T_e = pi T / sqrt(3)`` (rectangular window).

    With q = 1 (signal filling ``B``) both reduce to ``1 / (beta sqrt(B T g))`` and
    ``1 / (T_e sqrt(B T g))``. The q term is the extra noise-times-noise contribution
    when the correlation band is wider than the signal: the plain cross-correlator
    then integrates noise over ``B`` but signal curvature only over the signal band.

    noise_bw_hz: B in Hz (> 0). rms_bw_hz: B_rms in Hz (>= 0; 0 gives sigma_tdoa = inf,
    the delay of a CW is unobservable). T_s: integration time in s (> 0).
    g1, g2: linear input SNRs in B (>= 0; 0 gives inf).
    rho_tf: time-frequency correlation of the signal, dimensionless, -1 < rho_tf < 1
    (keyword only; 0 = no delay-Doppler coupling). Non-zero values use the coupled
    variances of the module docstring.
    Returns (sigma_tdoa_s, sigma_fdoa_hz).
    """
    bw, t = _positive(noise_bw_hz, "noise_bw_hz"), _positive(T_s, "T_s")
    rms = _nonneg(rms_bw_hz, "rms_bw_hz")
    a, b = _nonneg(g1, "g1"), _nonneg(g2, "g2")
    rho2, k = _coupling(rho_tf)
    with np.errstate(divide="ignore", invalid="ignore"):
        q = np.where(rms > 0.0, np.maximum(1.0, bw * bw / (12.0 * rms * rms)), 1.0)
        beta = 2.0 * math.pi * rms
        t_e = math.pi * t / math.sqrt(3.0)
        nn = np.where((a > 0.0) & (b > 0.0), 1.0 / (a * b), np.inf)  # noise-times-noise term
        extra = np.where(rho2 > 0.0, rho2 * (1.0 + q) * nn * k * k, 0.0)
        s_t = np.where(
            rms > 0.0, np.sqrt((_inverse_sum(a, b, q) * k + extra) / (2.0 * bw * t)) / beta, np.inf
        )
        s_f = np.sqrt((_inverse_sum(a, b) * k + extra) / (2.0 * bw * t)) / t_e
    return _out(s_t), _out(s_f)


def sigmas_from_coherence(
    C: Any,
    noise_bw_hz: Any,
    rms_bw_hz: Any,
    T_s: Any,
    noise_rms_bw_hz: Any | None = None,
    *,
    rho_tf: Any = 0.0,
    signal_noise_share: Any | None = None,
) -> tuple[Any, Any]:
    """One-sigma TDOA (s) and FDOA (Hz) from a measured coherence (MATH.md section 6 item 4).

    ``sigma_tdoa = sqrt((1 - C) / (2 B T C)) / beta``,
    ``sigma_fdoa = sqrt((1 - C) / (2 B T C)) / T_e``; identical to `stein_sigmas` with
    q = 1 because ``(1 - C) / C = 1/g1 + 1/g2 + 1/(g1 g2)``.

    C: magnitude-squared coherence of the normalised CAF peak, 0..1 (0 gives inf,
    1 gives 0). noise_bw_hz: B in Hz (> 0). rms_bw_hz: measured signal RMS bandwidth
    in Hz (>= 0; 0 gives sigma_tdoa = inf). T_s: integration time in s (> 0).
    noise_rms_bw_hz: optional RMS bandwidth in Hz, about the signal's spectral
    centroid, of the incoherent part of the cross-spectrum. When given, sigma_tdoa is
    multiplied by ``F = noise_rms_bw_hz / rms_bw_hz``. This is the data-driven form of the
    q term of `stein_sigmas` (the two agree for flat spectra) and equals 1 when the
    signal fills the correlation band.
    rho_tf: time-frequency correlation of the signal, dimensionless, -1 < rho_tf < 1
    (keyword only; 0 = no delay-Doppler coupling). With ``k = 1 / (1 - rho^2)`` and
    ``s`` = signal_noise_share the variances of the module docstring are
    ``(1 - C) / (2 B T C)`` times ``F^2 k + rho^2 E k^2`` (delay, over beta^2) and
    ``k + rho^2 E k^2`` (Doppler, over T_e^2), ``E = 2 (1 - s) + max(F^2 - 1, 0)``.
    signal_noise_share: the share ``s = (1/g1 + 1/g2) / ((1 - C) / C)`` of the
    signal-times-noise terms in the incoherent power, 0..1 (keyword only; only used
    with rho_tf != 0). It is not determined by C: it is 1 when one channel is free of
    noise (the case of MATH.md) and smallest, ``2 sqrt(C) / (1 + sqrt(C))``, when both
    channels have the same SNR. None assumes equal SNRs, which gives the largest
    sigmas any pair of SNRs with this coherence can have; a given value is raised to
    that minimum when it is below it.
    Returns (sigma_tdoa_s, sigma_fdoa_hz).
    """
    c = np.asarray(C, dtype=np.float64)
    if not np.all(np.isfinite(c)) or np.any(c < 0.0) or np.any(c > 1.0):
        raise ValueError(f"C must be within [0, 1] (got {C!r})")
    bw, t = _positive(noise_bw_hz, "noise_bw_hz"), _positive(T_s, "T_s")
    rms = _nonneg(rms_bw_hz, "rms_bw_hz")
    rho2, k = _coupling(rho_tf)
    factor = np.asarray(1.0)
    if noise_rms_bw_hz is not None:
        nrms = _nonneg(noise_rms_bw_hz, "noise_rms_bw_hz")
        with np.errstate(divide="ignore", invalid="ignore"):
            factor = np.where(rms > 0.0, nrms / rms, 1.0)
    root_c = np.sqrt(c)
    share_min = 2.0 * root_c / (1.0 + root_c)
    if signal_noise_share is None:
        share = share_min
    else:
        share = np.asarray(signal_noise_share, dtype=np.float64)
        if not np.all(np.isfinite(share)) or np.any(share < 0.0) or np.any(share > 1.0):
            raise ValueError(f"signal_noise_share must be within [0, 1] (got {signal_noise_share!r})")
        share = np.maximum(share, share_min)
    extra = rho2 * (2.0 * (1.0 - share) + np.maximum(factor * factor - 1.0, 0.0)) * k * k
    with np.errstate(divide="ignore", invalid="ignore"):
        base = np.where(c > 0.0, np.sqrt((1.0 - c) / (2.0 * bw * t * c)), np.inf)
        beta = 2.0 * math.pi * rms
        t_e = math.pi * t / math.sqrt(3.0)
        gain_t = np.sqrt(factor * factor * k + extra)
        gain_f = np.sqrt(k + extra)
        s_t = np.where((rms > 0.0) & (base > 0.0), base * gain_t / beta, np.where(rms > 0.0, 0.0, np.inf))
        s_f = np.where(base > 0.0, base * gain_f / t_e, 0.0)
    return _out(s_t), _out(s_f)
