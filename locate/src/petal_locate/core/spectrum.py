"""Spectrum estimate shared by the node monitor, the detector and the simulator."""
from __future__ import annotations

import numpy as np


def welch_psd_db(iq: np.ndarray, fs: float, nfft: int, navg: int) -> np.ndarray:
    """Welch power spectrum in dB (MATH.md section 7).

    iq: complex samples, at least nfft * navg of them (extra samples are ignored).
    fs: sample rate, Hz (only documents the bin spacing fs / nfft; not used in the maths).
    Returns float32[nfft], fft-shifted so bin k is at (k - nfft/2) * fs / nfft from the
    centre. navg non-overlapping Hann segments; power = mean |FFT(w x)|^2 / sum(w^2),
    so unit-variance white noise reads 0 dB in every bin.
    """
    if nfft < 2 or navg < 1:
        raise ValueError(f"nfft must be >= 2 and navg >= 1 (got nfft={nfft}, navg={navg})")
    x = np.asarray(iq)
    if x.shape[-1] < nfft * navg:
        raise ValueError(f"iq needs nfft * navg = {nfft * navg} samples, got {x.shape[-1]}")
    w = np.hanning(nfft + 1)[:-1]  # periodic Hann
    seg = x[: nfft * navg].reshape(navg, nfft) * w
    spec = np.fft.fftshift(np.fft.fft(seg, axis=1), axes=1)
    power = np.mean(spec.real**2 + spec.imag**2, axis=0) / np.sum(w * w)
    return (10.0 * np.log10(np.maximum(power, 1e-30))).astype(np.float32)
