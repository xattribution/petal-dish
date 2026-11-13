"""Cross-ambiguity estimator for TDOA / FDOA (MATH.md section 6).

    chi(tau, nu) = sum_n  y_A(t_n + tau) * conj(y_B(t_n)) * exp(-j 2 pi nu t_n)

`caf_estimate` finds the peak of ``|chi|^2`` inside a caller-supplied delay / Doppler
window and returns the delay (TDOA, A minus B, seconds), the Doppler (FDOA, A minus
B, hertz), their one-sigma errors, and the detection and ambiguity flags.

Array convention: ``ya[m]`` is channel A at labelled time ``tA + m / fs`` and
``yb[n]`` is channel B at ``tB + n / fs``; ``t0_offset_s = tA - tB``. A peak at array
lag ``l`` samples (``ya[n + l]`` against ``yb[n]``) is the true delay
``l / fs + t0_offset_s``. Every delay the caller passes in or gets back
(``delay_window``, ``exclude``, ``mirror_window``, axes, ``tdoa_s``) is a true A-minus-B
value; array lags never leave this module. The estimate refers to the midpoint of the
channel-B samples (``t_mid`` of MATH.md section 5).

How it works, in the order of MATH.md section 6:

0. Both channels pass one linear-phase FIR (`_band_filter`); ``B`` is its noise bandwidth.
1. Coarse search (`_coarse_surface`): each segment of B is correlated against a
   stretch of A longer by the delay window (never circular segment correlation), an
   FFT across segments gives Doppler. The surface covers the window plus a guard band,
   on integer lags; a half-lag statistic catches peaks that fall between two lags.
3. Noise floor (`_fit_floor`): median of sampled cells over ln 2, shaped across
   Doppler by the band filter (`_FloorShape`), away from peaks and notches.
5. Notches and peaks (`_detect`): exclusion zones remove a main lobe and discount the
   predicted sidelobes of a visible self-peak; distinct peaks are extracted strongest
   first, each discounting its own sidelobes, so a sidelobe is never a second peak.
2. Fine search (`_refine`): Newton on the exact CAF in delay and Doppler (fractional
   delay by FFT phase ramp, exact derivatives), no interpolation of the coarse grid.
4. Sigmas from the normalised peak (`crlb.sigmas_from_coherence`), the RMS bandwidth
   of the correlated signal, the RMS bandwidth of the incoherent residue (the data-driven
   q term of MATH.md section 8) and the curvature (`rho_tf`).
6. to 8. Mirror window, display patch and cuts, ambiguity rule: `caf_estimate`.

The module is pure: no I/O, no global state, no random numbers, one thread.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence

import numpy as np
from scipy import fft as sfft
from scipy import ndimage
from scipy import signal as ssignal

from .crlb import sigmas_from_coherence

__all__ = [
    "CafResult",
    "caf_estimate",
    "REASON_BELOW_THRESHOLD",
    "REASON_AMBIGUOUS",
    "REASON_COUPLED",
    "REASON_NARROWBAND",
    "REASON_OUTSIDE",
]

REASON_BELOW_THRESHOLD = "below threshold"
"""No peak in the primary window reaches ``min_snr_db``."""
REASON_AMBIGUOUS = "ambiguous"
"""A second peak within 3 dB of the strongest lies in the primary window."""
REASON_COUPLED = "delay-doppler coupled"
"""The delay and Doppler errors are correlated by more than 0.9."""
REASON_NARROWBAND = "narrowband"
"""Detected, FDOA valid, but the delay is unobservable (flat delay cut or sigma too large)."""
REASON_OUTSIDE = "peak outside window"
"""The only cells above threshold are the skirt of a peak outside the primary window."""

_LN2 = math.log(2.0)
_MIN_SAMPLES = 256  # shortest capture (after band limiting) the estimator accepts
_K_MIN = 64  # minimum number of Doppler segments
_STAT_LAGS = 64  # minimum number of lags the surface spans (noise and flatness statistics)
_PRE_DB = 1.0  # coarse candidates are kept down to min_snr_db - _PRE_DB (grid losses)
_REFINE_SPAN_DB = 7.5  # candidates within this of the strongest are refined
_MAX_CAND = 8
_SIDELOBE_KEEP = 4.0  # a second peak must exceed 4x a stronger peak's sidelobe envelope
_EXCL_MARGIN = 2.0  # safety factor on the predicted sidelobe envelope of an excluded peak
_PED_FRACTION = 0.8  # a peak less than this fraction of the threshold above its ridge is flat in delay
_GUARD_LAGS = 24  # lags searched beyond the delay window (plus 8 main-lobe widths)
_GUARD_BINS = 64  # Doppler bins searched beyond the window (16 / T)
_NUMERIC_FLOOR = 2e-4  # smallest sigma reported, in samples (delay) and in cycles over T (Doppler)
_SEED_SIGMA = 5.0  # a stretch of the cross-spectrum counts as signal only with a value this significant
_SHARE_BLOCKS = 32  # time blocks of the cross-spectrogram that splits the incoherent power
_SHARE_MIN_RHO = 0.05  # below this coupling the split does not matter (under 0.5 % of a sigma)
_AMBIGUITY_DB = 3.0
_MIRROR_DB = 6.0
_COUPLING_LIMIT = 0.9
_PATCH = 65
_CUT_POINTS = 4096
_FLOOR_DB = -30.0
_MAX_CELLS = 1_000_000


@dataclass
class CafResult:
    """Result of `caf_estimate`.

    tdoa_s: delay of the peak, true A minus B, seconds; None when not detected,
        ambiguous, delay-Doppler coupled, or the signal is too narrow for delay.
    fdoa_hz: Doppler of the peak, A minus B, hertz; None when not detected, ambiguous
        or coupled.
    sigma_tdoa_s, sigma_fdoa_hz: one-sigma errors (s, Hz) from the normalised peak;
        None when not detected or not finite (for example the delay of a CW). Never
        below 2e-4 sample and 2e-4 / T, the numerical accuracy of the fine search.
        Delay-Doppler coupling widens them as `crlb.sigmas_from_coherence` describes
        (more than MATH.md's 1 / sqrt(1 - rho_tf^2) when both channels are noisy).
    rho_tf: delay-Doppler correlation of the curvature of ``|chi|^2`` at the peak
        (-1..1; MATH.md section 6 item 4), positive when the Doppler error follows the
        delay error; 0.0 when not available. It is the time-frequency correlation of
        the waveform (1 / m for m linear sweeps across the capture) and equals the
        correlation of the two errors when one channel is free of noise; with noise in
        both channels the errors correlate more strongly (about 0.7 against rho_tf 0.48
        at two equal SNRs of -3 dB, see `crlb`). It decides "delay-doppler coupled".
    snr_db: post-correlation SNR of the strongest admissible peak, 10 log10 of peak
        power over the mean off-peak power of the surface.
    coherence: ``|chi_peak|^2 / (sum |y_A|^2 sum |y_B|^2)`` over the samples in the
        sum (0..1), in the correlation band.
    detected: ``snr_db >= min_snr_db``.
    mirror_ambiguous: a distinct peak in ``mirror_window`` is not more than 6 dB
        below the primary peak.
    candidates: up to 8 distinct peaks ``(tdoa_s, fdoa_hz, snr_db)``, strongest first
        (primary-window peaks, then a mirror-window peak when one was found).
    rms_bandwidth_hz: measured RMS bandwidth of the correlated signal, Hz; None when
        not detected or when the delay cut is flat (nothing to measure it from).
    surface: float32 [n_delay <= 65, n_doppler <= 65], dB relative to the noise floor,
        patch of the coarse surface around the peak; ``surface[i, j]`` is at
        ``delay_axis_s[i]``, ``doppler_axis_hz[j]``.
    delay_axis_s, doppler_axis_hz: axes of the patch (true A-minus-B seconds, hertz).
    delay_cut: ``(axis_s, dB)`` through the peak Doppler across the delay window.
    doppler_cut: ``(axis_hz, dB)`` through the peak delay across the Doppler window.
        Cuts longer than 4096 points are reduced by taking the maximum of each group.
    reason: None for a complete result, otherwise one of the ``REASON_*`` strings.
        With ``REASON_OUTSIDE`` ``snr_db`` is that of the strongest admissible cell
        left, although a stronger peak exists just outside the window.
    noise_bandwidth_hz: ``B`` of MATH.md section 8 actually used: the noise bandwidth
        of the band filter, or ``fs`` when no filter was applied.
    integration_s: ``T``: samples in the correlation sum at the peak / fs.
    """

    tdoa_s: float | None
    fdoa_hz: float | None
    sigma_tdoa_s: float | None
    sigma_fdoa_hz: float | None
    rho_tf: float
    snr_db: float
    coherence: float
    detected: bool
    mirror_ambiguous: bool
    candidates: list[tuple[float, float, float]]
    rms_bandwidth_hz: float | None
    surface: np.ndarray
    delay_axis_s: np.ndarray
    doppler_axis_hz: np.ndarray
    delay_cut: tuple[np.ndarray, np.ndarray]
    doppler_cut: tuple[np.ndarray, np.ndarray]
    reason: str | None
    noise_bandwidth_hz: float = 0.0
    integration_s: float = 0.0


# --------------------------------------------------------------------------- helpers


def _abs2(z: np.ndarray) -> np.ndarray:
    return z.real * z.real + z.imag * z.imag


def _envelope(u: np.ndarray) -> np.ndarray:
    """Peak envelope of sinc^2(u): 1 inside the main lobe, 1 / (pi u)^2 outside."""
    with np.errstate(divide="ignore"):
        return np.minimum(1.0, 1.0 / (math.pi * np.abs(u)) ** 2)


def _prev_fast_len(n: int) -> int:
    return int(sfft.prev_fast_len(n, real=False))


def _overlap(na: int, nb: int, lag: object) -> np.ndarray:
    """Number of products ``ya[n + lag] conj(yb[n])`` inside both captures, per array lag (samples)."""
    lag_i = np.asarray(lag, dtype=np.int64)
    return np.maximum(np.minimum(nb, na - lag_i) - np.maximum(0, -lag_i), 0)


def _check_iq(x: object, name: str) -> np.ndarray:
    arr = np.asarray(x)
    if arr.ndim != 1:
        raise ValueError(f"{name} must be a 1-D array of complex samples (got shape {arr.shape})")
    if arr.size < _MIN_SAMPLES:
        raise ValueError(f"{name} needs at least {_MIN_SAMPLES} samples (got {arr.size})")
    if not (np.issubdtype(arr.dtype, np.number)):
        raise ValueError(f"{name} must be numeric (got dtype {arr.dtype})")
    arr = arr.astype(np.complex128)
    if not (np.isfinite(arr.real).all() and np.isfinite(arr.imag).all()):
        raise ValueError(f"{name} contains non-finite samples")
    if not np.any(arr):
        raise ValueError(f"{name} has zero power")
    return arr


def _check_scalar(value: object, name: str) -> float:
    """A real number (not a string, not None), as float; ValueError naming the argument otherwise."""
    if isinstance(value, (str, bytes)) or value is None:
        raise ValueError(f"{name} must be a number (got {value!r})")
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number (got {value!r})") from exc


def _check_window(w: object, name: str) -> tuple[float, float]:
    try:
        lo, hi = (float(v) for v in w)  # type: ignore[union-attr]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a (min, max) pair of numbers") from exc
    if not (math.isfinite(lo) and math.isfinite(hi)) or hi < lo:
        raise ValueError(f"{name} must be finite with min <= max (got {w!r})")
    return lo, hi


def _band_filter(fs: float, pass_hz: float, max_taps: int) -> tuple[np.ndarray, float] | None:
    """Linear-phase low-pass passing +/- pass_hz / 2 (Hz); None when nothing can be removed.

    Returns (taps, noise bandwidth in Hz). Kaiser design, 60 dB stop band when the
    capture is long enough, transition 5 % of the pass band (or what fits below fs/2).
    """
    f_pass = 0.5 * pass_hz
    room = 0.5 * fs - f_pass
    if room <= 0.01 * fs:
        return None
    width = min(max(0.05 * pass_hz, 4.0 * fs / max_taps), room)
    ntaps, beta = ssignal.kaiserord(60.0, width / (0.5 * fs))
    ntaps |= 1
    if ntaps > max_taps:
        ntaps = max_taps | 1
        atten = 2.285 * (ntaps - 1) * math.pi * width / (0.5 * fs) + 7.95
        beta = ssignal.kaiser_beta(atten)
    taps = ssignal.firwin(ntaps, f_pass + 0.5 * width, window=("kaiser", beta), fs=fs)
    noise_bw = fs * float(np.sum(taps * taps)) / float(np.sum(taps)) ** 2
    return taps, noise_bw


# --------------------------------------------------------------------- coarse search


@dataclass
class _Coarse:
    """Coarse |chi|^2 surface on integer array lags and a zero-padded Doppler grid.

    At array lag l only ``_overlap(na, nb, l)`` products exist, so a peak (overlap
    squared) and the noise floor (overlap) both depend on the lag. Every row is
    therefore scaled by ``ref / overlap``: the floor is the same on every row and a
    cell divided by it is the post-correlation SNR for the samples of that row.
    """

    power: np.ndarray  # float32 [n_lags, n_cols], rows scaled to `ref` products
    ref: float  # number of products every row is scaled to (the length of channel B)
    lag0: int  # array lag of row 0, samples
    bin0: int  # signed Doppler bin of column 0 (bin * dnu + nu_c = Doppler in Hz)
    dnu: float  # Doppler bin spacing, Hz
    nu_c: float  # Doppler-window centre, Hz
    seg: int  # segment length, samples
    stat0: np.ndarray  # float32 [rows, n_stat]: samples of the integer-lag statistic
    stat1: np.ndarray  # float32 [rows, n_stat]: samples of the half-lag statistic
    stat_bins: np.ndarray  # signed Doppler bins of the statistic columns
    half_val: np.ndarray  # half-lag statistic values worth a look
    half_row: np.ndarray  # row r: the cell sits at lag0 + r + 0.5
    half_col: np.ndarray


def _coarse_surface(
    a: np.ndarray,
    b: np.ndarray,
    fs: float,
    lag0: int,
    n_lags: int,
    nu_lo: float,
    nu_hi: float,
    collect_level: float,
) -> _Coarse:
    """MATH.md section 6 item 1.

    a, b: band-limited channels (complex128). lag0, n_lags: integer array lags
    covered (delay window plus guard). nu_lo, nu_hi: Doppler window, Hz; a guard of
    _GUARD_BINS bins is kept on each side. collect_level: half-lag cells (rows r and
    r + 1 summed) above this many noise floors are kept.
    """
    na, nb = a.size, b.size
    nu_c = 0.5 * (nu_lo + nu_hi)
    nu_half = 0.5 * (nu_hi - nu_lo)
    seg_max = nb if nu_half <= 0.0 else int(math.floor(0.25 * fs / nu_half))
    if seg_max < 1:
        raise ValueError(
            f"doppler_window is wider than fs / 2 (half-width {nu_half:g} Hz at fs {fs:g} Hz)"
        )
    seg = max(1, min(seg_max, nb // _K_MIN))
    k_seg = -(-nb // seg)
    n_dop = int(sfft.next_fast_len(max(4 * k_seg, 256), real=False))
    dnu = fs / (seg * n_dop)
    h = min(int(math.ceil(nu_half / dnu)) + 2 + _GUARD_BINS, n_dop // 2 - 1)
    h_stat = min(n_dop // 2 - 1, max(2 * h, 128))

    # Doppler-window centre removed from A; single precision is ample for the search.
    rot = np.exp((-2j * math.pi * nu_c / fs) * np.arange(na))
    a_c = (a * rot).astype(np.complex64)
    stretch = seg + n_lags - 1
    a_pad = np.zeros((k_seg - 1) * seg + stretch, dtype=np.complex64)
    src_lo, src_hi = max(lag0, 0), min(lag0 + a_pad.size, na)
    if src_hi > src_lo:
        a_pad[src_lo - lag0 : src_hi - lag0] = a_c[src_lo:src_hi]
    b_pad = np.zeros(k_seg * seg, dtype=np.complex64)
    b_pad[:nb] = b
    windows = np.lib.stride_tricks.sliding_window_view(a_pad, stretch)[::seg]
    segments = b_pad.reshape(k_seg, seg)

    # Each B segment against its A stretch: X[k, w] = sum_n a[k L + lag0 + w + n] conj(b[k L + n]).
    nfft = int(sfft.next_fast_len(stretch, real=False))
    x = np.empty((k_seg, n_lags), dtype=np.complex64)
    batch = max(1, (1 << 21) // nfft)
    for s in range(0, k_seg, batch):
        fa = sfft.fft(windows[s : s + batch], n=nfft, axis=1)
        fb = sfft.fft(segments[s : s + batch], n=nfft, axis=1)
        np.conjugate(fb, out=fb)
        fa *= fb
        x[s : s + batch] = sfft.ifft(fa, axis=1)[:, :n_lags]

    # FFT across segments for Doppler, a block of lags at a time. Each lag is scaled
    # (in amplitude) to the same number of products, see `_Coarse`.
    ref = float(nb)
    norm = np.sqrt(ref / np.maximum(_overlap(na, nb, lag0 + np.arange(n_lags)), 1)).astype(np.float32)
    sel = np.arange(-h, h + 1) % n_dop
    stat_step = max(1, (2 * h_stat + 1) // 512)
    stat_bins = np.arange(-h_stat, h_stat + 1, stat_step)
    stat_sel = stat_bins % n_dop
    chunk = max(1, (1 << 22) // n_dop)
    row_step = max(1, min(n_lags // 256, chunk))
    power = np.empty((n_lags, sel.size), dtype=np.float32)
    stat0: list[np.ndarray] = []
    stat1: list[np.ndarray] = []
    half_val: list[np.ndarray] = []
    half_row: list[np.ndarray] = []
    half_col: list[np.ndarray] = []
    for w0 in range(0, n_lags, chunk):
        w1 = min(w0 + chunk, n_lags)
        w1x = min(w1 + 1, n_lags)  # one more lag for the half-lag statistic
        f = sfft.fft(np.ascontiguousarray(x[:, w0:w1x].T), n=n_dop, axis=1)
        f *= norm[w0:w1x, None]
        fw = f[:, sel]
        power[w0:w1] = _abs2(fw[: w1 - w0])
        n_pair = min(w1 - w0, f.shape[0] - 1)
        rows = np.arange((-w0) % row_step, w1 - w0, row_step)
        fstat = f[:, stat_sel]
        stat0.append(_abs2(fstat[rows]))
        rows1 = rows[rows < n_pair]
        if rows1.size:
            stat1.append(_abs2(fstat[rows1] + fstat[rows1 + 1]))
        # Half-lag statistic: a peak midway between two lags loses up to 3.9 dB on the
        # integer-lag grid; the sum of adjacent lags recovers all but about 0.8 dB.
        p_lo, p_hi = 0, n_pair
        if p_hi > p_lo and stat1:
            floor1 = float(np.median(np.concatenate([s.ravel() for s in stat1]))) / _LN2
            p1 = _abs2(fw[p_lo:p_hi] + fw[p_lo + 1 : p_hi + 1])
            idx = np.flatnonzero(p1 >= collect_level * floor1)
            if idx.size > _MAX_CELLS // 8:
                flat = p1.ravel()
                idx = idx[np.argpartition(flat[idx], idx.size - _MAX_CELLS // 8)[-(_MAX_CELLS // 8) :]]
            if idx.size:
                r, c = np.divmod(idx, p1.shape[1])
                half_val.append(p1[r, c])
                half_row.append(r + (w0 + p_lo))
                half_col.append(c)

    def _cat(parts: list[np.ndarray], dtype: type) -> np.ndarray:
        return np.concatenate(parts) if parts else np.empty(0, dtype=dtype)

    return _Coarse(
        power=power,
        ref=ref,
        lag0=lag0,
        bin0=-h,
        dnu=dnu,
        nu_c=nu_c,
        seg=seg,
        stat0=np.vstack(stat0),
        stat1=np.vstack(stat1) if stat1 else np.empty((0, stat_bins.size), dtype=np.float32),
        stat_bins=stat_bins,
        half_val=_cat(half_val, np.float32),
        half_row=_cat(half_row, np.int64),
        half_col=_cat(half_col, np.int64),
    )


# ------------------------------------------------------------------ peak bookkeeping


@dataclass
class _Excl:
    """A predicted self-peak, measured exactly at its Doppler."""

    lag: float  # array lag, samples
    nu: float  # Hz
    power: float  # |chi|^2 at the predicted point, scaled like the coarse surface
    cut: np.ndarray  # |chi|^2 against integer lag at this Doppler, scaled like the coarse surface
    cut_lag0: int  # array lag of cut[0]


@dataclass
class _Lobe:
    lag: float
    nu: float
    snr: float  # linear, for the sidelobe envelope (0 when too weak to matter)
    bw: float  # effective bandwidth of the delay main lobe, Hz (0 = flat in delay)


@dataclass
class _Cand:
    lag: float  # array lag, samples (coarse)
    nu: float  # Hz (coarse)
    snr: float  # linear post-correlation SNR on the coarse grid
    eff: float  # same after subtracting predicted sidelobes of excluded peaks
    bw: float  # effective bandwidth of the delay main lobe, Hz
    flat: bool  # delay cut flat on the coarse grid
    pedestal: float  # median of the delay cut at this Doppler, in noise floors
    col: int
    inside: bool  # the coarse cell lies in the search window (not in its guard band)
    ridge_ratio: float = 1.0  # ridge power around the peak lag over the peak cell (flat cuts)


class _FloorShape:
    """Relative noise floor of the coarse surface against Doppler.

    Without a band filter the product noise is white and the floor is flat. With one,
    the spectrum of ``y_A(t + tau) conj(y_B(t))`` is the correlation of the two channel
    spectra: for noise filling the filter that is ``|H|^2`` correlated with itself, a
    triangle peaking at zero Doppler, which matters as soon as the filter is not much
    wider than the Doppler window. The segment sums of the coarse search then fold it:
    floor(nu) = sum_m s(nu + m fs / L) sinc^2((nu - nu_c) L / fs + m).
    """

    def __init__(self, taps: np.ndarray | None, fs: float) -> None:
        self.fs = fs
        self.grid: np.ndarray | None = None
        if taps is not None:
            r = ssignal.fftconvolve(taps, taps[::-1])  # autocorrelation of the taps <-> |H|^2
            w = r * r  # <-> |H|^2 correlated with itself across frequency
            n_grid = int(sfft.next_fast_len(max(8 * w.size, 4096), real=False))
            buf = np.zeros(n_grid)
            c = taps.size - 1
            buf[: c + 1] = w[c:]
            buf[n_grid - c :] = w[:c]
            spec = sfft.fft(buf).real
            self.grid = np.append(spec, spec[0]) / spec[0]

    def _s(self, nu: np.ndarray) -> np.ndarray:
        assert self.grid is not None
        pos = np.mod(nu / self.fs, 1.0) * (self.grid.size - 1)
        i = np.minimum(pos.astype(np.int64), self.grid.size - 2)
        frac = pos - i
        return np.maximum(self.grid[i] * (1.0 - frac) + self.grid[i + 1] * frac, 0.0)

    def at(self, nu: object, nu_c: float, seg: int) -> np.ndarray:
        """Relative floor at absolute Doppler nu (Hz) for segment length seg (samples)."""
        v = np.asarray(nu, dtype=np.float64)
        if self.grid is None:
            return np.ones_like(v)
        total = np.zeros_like(v)
        weight = np.zeros_like(v)
        for m in range(-8, 9):
            wgt = np.sinc((v - nu_c) * (seg / self.fs) + m) ** 2
            total += wgt * self._s(v + m * self.fs / seg)
            weight += wgt
        return np.maximum(total / weight, 1e-3)


@dataclass
class _Floor:
    """Mean off-peak |chi|^2 (raw units) of one coarse surface as a function of Doppler."""

    scale: float  # floor where the shape is 1
    ratio1: float  # half-lag statistic floor over integer-lag floor
    shape: _FloorShape
    nu_c: float
    seg: int

    def at(self, nu: object) -> np.ndarray:
        """Floor of the integer-lag statistic at absolute Doppler nu (Hz)."""
        return self.scale * self.shape.at(nu, self.nu_c, self.seg)


def _fit_floor(co: _Coarse, shape: _FloorShape, keep: np.ndarray) -> _Floor:
    """Noise floor from sampled surface cells: chi-square(2) cells, mean = median / ln 2.

    keep: statistic columns that may be used (those away from visible peaks).
    """
    stat_nu = co.nu_c + co.stat_bins * co.dnu
    rel = shape.at(stat_nu, co.nu_c, co.seg)
    scale = float(np.median(co.stat0[:, keep] / rel[None, keep])) / _LN2
    if not scale > 0.0:
        raise ValueError("ya / yb do not overlap enough inside delay_window to estimate a noise floor")
    ratio1 = 2.0
    if co.stat1.size:
        ratio1 = float(np.median(co.stat1[:, keep] / rel[None, keep])) / _LN2 / scale
    return _Floor(scale, ratio1, shape, co.nu_c, co.seg)


@dataclass
class _Search:
    coarse: _Coarse
    floor: _Floor
    cands: list[_Cand]
    notches: list[_Lobe]
    rows: tuple[int, int]  # window rows (inclusive)
    cols: tuple[int, int]  # window columns (inclusive)
    lag_win: tuple[float, float]  # array-lag window, samples
    nu_win: tuple[float, float]  # Hz


def _excess(snr_pk: float, pedestal: float, gain: float = 1.0) -> float:
    """Squared amplitude by which a peak exceeds the ridge it sits on, in noise floors.

    A narrow-band signal gives a ridge along delay of power A^2 = pedestal - 1 (the
    median of the delay cut, less the noise); a cell of it reads |A + n|^2, so the
    strongest of M cells exceeds A in amplitude by about sqrt(ln M). A peak whose
    excess (sqrt(snr) - A)^2 is below the detection threshold is such a fluctuation
    and carries no delay information. For a wide-band signal the pedestal is the noise
    floor, A = 0, and the excess is the peak SNR itself.

    gain: power gain of the peak statistic on a ridge relative to the cut the pedestal
    was taken from (the half-lag statistic adds two lags coherently: up to 4 / ratio1).
    """
    return (math.sqrt(max(snr_pk, 0.0)) - math.sqrt(max(gain * (pedestal - 1.0), 0.0))) ** 2


def _lobe_shape(
    cut_snr: np.ndarray, i_pk: int, snr_pk: float, fs: float, bw_max: float, ped_thr: float, gain: float = 1.0
) -> tuple[float, bool, float]:
    """Effective bandwidth (Hz) of the delay main lobe, flatness, and the cut's median.

    cut_snr: delay cut in noise floors on integer lags. The cut is flat when the peak
    does not stand significantly above the cut's median (a ridge plus noise, see
    `_excess`), or when it never falls 6 dB below the peak. Otherwise the bandwidth
    follows from the -6 dB half-width (0.6 / B for a flat spectrum), never above
    bw_max; a signal narrower than the correlation band has a wider main lobe.
    """
    med = float(np.median(cut_snr))
    if _excess(snr_pk, med, gain) < ped_thr:
        return 0.0, True, med
    dil = max(2, int(math.ceil(fs / bw_max)))
    c = ndimage.maximum_filter1d(cut_snr, size=2 * dil + 1, mode="nearest")
    below = c < max(0.25 * snr_pk, 6.0)
    i_pk = int(min(max(i_pk, 0), cut_snr.size - 1))
    right = np.flatnonzero(below[i_pk:])
    left = np.flatnonzero(below[i_pk::-1])
    extents = [int(e[0]) for e in (right, left) if e.size]
    if not extents:
        return 0.0, True, med
    # The dilated cut first drops below the level dil + 1 lags after the last cell
    # above it; the -6 dB point lies half a lag beyond that cell.
    half_width = max(max(extents) - dil - 0.5, 0.5)
    return min(bw_max, 0.6 * fs / half_width), False, med


def _in_lobe(dlag: np.ndarray, dnu: np.ndarray, bw: float, fs: float, t_int: float, dnu_bin: float) -> np.ndarray:
    """Inside the main lobe (first nulls: 1 / bw in delay, 1 / T in Doppler), half a cell of slack."""
    in_nu = np.abs(dnu) < 1.0 / t_int + 0.5 * dnu_bin
    if bw <= 0.0:
        return in_nu
    return in_nu & (np.abs(dlag) < fs / bw + 0.5)


def _sidelobes(dlag: np.ndarray, dnu: np.ndarray, lobe: _Lobe, fs: float, t_int: float) -> np.ndarray:
    """Predicted sidelobe envelope of a peak, in noise floors, at offsets (samples, Hz)."""
    env = _envelope(dnu * t_int) * lobe.snr
    if lobe.bw > 0.0:
        env = env * _envelope(dlag * (lobe.bw / fs))
    return env


def _detect(
    co: _Coarse,
    shape: _FloorShape,
    fs: float,
    t_int: float,
    noise_bw: float,
    lag_win: tuple[float, float],
    nu_win: tuple[float, float],
    excl: Sequence[_Excl],
    thr_pre: float,
) -> _Search:
    """Noise floor, exclusion notches and distinct peaks of a coarse surface."""
    power = co.power
    n_lags, n_cols = power.shape
    stat_nu = co.nu_c + co.stat_bins * co.dnu
    floor = _fit_floor(co, shape, np.ones(stat_nu.size, dtype=bool))
    col_nu = co.nu_c + (co.bin0 + np.arange(n_cols)) * co.dnu
    col_floor = floor.at(col_nu)

    r_lo = max(int(math.ceil(lag_win[0] - 0.5 - co.lag0)), 0)
    r_hi = min(int(math.floor(lag_win[1] + 0.5 - co.lag0)), n_lags - 1)
    c_lo = max(int(math.ceil((nu_win[0] - co.nu_c) / co.dnu - 0.5)) - co.bin0, 0)
    c_hi = min(int(math.floor((nu_win[1] - co.nu_c) / co.dnu + 0.5)) - co.bin0, n_cols - 1)
    # Cells worth looking at, over the window and its guard band (a peak just outside
    # the window throws sidelobes into it): integer-lag cells within 6 dB of the
    # pre-threshold (the top few when there are none) ...
    collect = 0.25 * thr_pre
    idx = np.flatnonzero(power >= (collect * col_floor).astype(np.float32)[None, :])
    if idx.size == 0:
        norm = (power / col_floor.astype(np.float32)[None, :]).ravel()
        k = min(64, norm.size)
        idx = np.argpartition(norm, norm.size - k)[norm.size - k :]
    elif idx.size > _MAX_CELLS:
        flat = power.ravel()
        idx = idx[np.argpartition(flat[idx], idx.size - _MAX_CELLS)[-_MAX_CELLS:]]
    r, col = np.divmod(idx, n_cols)
    lag = (co.lag0 + r).astype(np.float64)
    snr = power[r, col].astype(np.float64) / col_floor[col]
    # ... and half-lag cells.
    if co.half_val.size:
        half_snr = co.half_val.astype(np.float64) / (floor.ratio1 * col_floor[co.half_col])
        keep = half_snr >= collect
        if np.any(keep):
            lag = np.concatenate([lag, co.lag0 + co.half_row[keep] + 0.5])
            col = np.concatenate([col, co.half_col[keep]])
            snr = np.concatenate([snr, half_snr[keep]])
    inside = (lag >= lag_win[0] - 0.5) & (lag <= lag_win[1] + 0.5) & (col >= c_lo) & (col <= c_hi)
    if not np.any(inside) and r_hi >= r_lo and c_hi >= c_lo:
        # Nothing in the window came near the threshold: take its strongest cells
        # anyway, so that a non-detection can report its level.
        sub = power[r_lo : r_hi + 1, c_lo : c_hi + 1] / col_floor[None, c_lo : c_hi + 1]
        k = min(64, sub.size)
        top = np.argpartition(sub.ravel(), sub.size - k)[sub.size - k :]
        r_top, c_top = np.divmod(top, sub.shape[1])
        lag = np.concatenate([lag, (co.lag0 + r_lo + r_top).astype(np.float64)])
        col = np.concatenate([col, c_lo + c_top])
        snr = np.concatenate([snr, sub[r_top, c_top]])
        inside = np.concatenate([inside, np.ones(k, dtype=bool)])
    nu = col_nu[col]

    # Exclusion notches: main lobe removed, and the predicted sidelobes of a visible
    # self-peak subtracted in amplitude before thresholding.
    notches: list[_Lobe] = []
    eff = snr.copy()
    for ex in excl:
        floor_x = float(floor.at(ex.nu))
        snr_x = ex.power / floor_x
        bw_x, snr_env = noise_bw, 0.0
        if snr_x >= thr_pre:
            i_pk = int(round(ex.lag)) - ex.cut_lag0
            bw_x, _, _ = _lobe_shape(ex.cut / floor_x, i_pk, snr_x, fs, noise_bw, _PED_FRACTION * thr_pre)
            snr_env = snr_x
        lobe = _Lobe(ex.lag, ex.nu, snr_env, bw_x)
        notches.append(lobe)
        dl, dn = lag - ex.lag, nu - ex.nu
        if snr_env > 0.0:
            env = _EXCL_MARGIN * _sidelobes(dl, dn, lobe, fs, t_int)
            eff = np.maximum(np.sqrt(eff) - np.sqrt(env), 0.0) ** 2
        eff[_in_lobe(dl, dn, bw_x, fs, t_int, co.dnu)] = 0.0

    # Greedy extraction of the distinct peaks above the pre-threshold, strongest first.
    cands: list[_Cand] = []
    alive = eff > 0.0
    while len(cands) < 4 * _MAX_CAND and np.any(alive):
        i = int(np.argmax(np.where(alive, eff, -1.0)))
        if eff[i] < thr_pre:
            break
        cand = _Cand(
            float(lag[i]), float(nu[i]), float(snr[i]), float(eff[i]), noise_bw, False, 1.0, int(col[i]), bool(inside[i])
        )
        cands.append(cand)
        row = int(math.floor(lag[i])) - co.lag0
        cut = power[:, cand.col].astype(np.float64) / col_floor[cand.col]
        gain = 4.0 / floor.ratio1 if cand.lag != math.floor(cand.lag) else 1.0
        cand.bw, cand.flat, cand.pedestal = _lobe_shape(cut, row, cand.snr, fs, noise_bw, _PED_FRACTION * thr_pre, gain)
        if cand.flat and not cand.inside and c_lo <= cand.col <= c_hi and r_hi >= r_lo:
            # A ridge along delay found in the guard band runs through the window
            # too: take its strongest cell inside (the delay of a ridge is arbitrary).
            row = r_lo + int(np.argmax(cut[r_lo : r_hi + 1]))
            cand.lag, cand.snr, cand.inside = float(co.lag0 + row), float(cut[row]), True
            cand.eff = min(cand.eff, cand.snr)
        if cand.flat:
            # How far noise lifted this cell above the ridge it belongs to: the median
            # of the neighbouring lags (same overlap, same grid) less the noise floor,
            # over the cell itself.
            local = cut[max(row - 32, 0) : row + 34]
            cell = float(np.max(cut[max(row, 0) : row + 2]))
            if cell > 0.0:
                cand.ridge_ratio = min(max((float(np.median(local)) - 1.0) / cell, 0.0), 1.0)
        # A weaker cell is a separate peak only if it stands clear of this peak's
        # sidelobes: well above their envelope, and still above the pre-threshold
        # after the envelope is subtracted in amplitude (sidelobe plus noise).
        dl, dn = lag - lag[i], nu - nu[i]
        side = _sidelobes(dl, dn, _Lobe(float(lag[i]), cand.nu, float(snr[i]), cand.bw), fs, t_int)
        eff = np.maximum(np.sqrt(eff) - np.sqrt(side), 0.0) ** 2
        alive &= (snr > _SIDELOBE_KEEP * side) & (eff > 0.0) & ~_in_lobe(dl, dn, cand.bw, fs, t_int, co.dnu)
        alive[i] = False
    # The strongest cell left in the window, so that a non-detection reports its level.
    rest = alive & inside
    if np.any(rest):
        i = int(np.argmax(np.where(rest, eff, -1.0)))
        cands.append(
            _Cand(float(lag[i]), float(nu[i]), float(snr[i]), float(eff[i]), noise_bw, False, 1.0, int(col[i]), True)
        )

    # Noise floor again, away from the Doppler columns of every visible peak and notch.
    keep_cols = np.ones(stat_nu.size, dtype=bool)
    strong = [(cd.nu, cd.snr) for cd in cands if cd.eff >= thr_pre]
    strong += [(lb.nu, lb.snr) for lb in notches if lb.snr > 0.0]
    for nu_p, snr_p in strong:
        half_width = max(2.0, 2.0 * math.sqrt(snr_p) / math.pi) / t_int
        keep_cols &= np.abs(stat_nu - nu_p) > half_width
    if strong and np.count_nonzero(keep_cols) >= max(16, stat_nu.size // 5):
        refit = _fit_floor(co, shape, keep_cols)
        for cd in cands:
            scale = float(floor.at(cd.nu) / refit.at(cd.nu))
            if cd.lag != math.floor(cd.lag):
                scale *= floor.ratio1 / refit.ratio1
            cd.snr *= scale
            cd.eff *= scale
            cd.pedestal *= float(floor.at(cd.nu) / refit.at(cd.nu))
        for lb in notches:
            lb.snr *= float(floor.at(lb.nu) / refit.at(lb.nu))
        floor = refit
    cands.sort(key=lambda cd: -cd.eff)
    return _Search(co, floor, cands, notches, (r_lo, r_hi), (c_lo, c_hi), lag_win, nu_win)


# ----------------------------------------------------------------------- fine search


class _Aligned:
    """Channels overlapped at an integer array lag, for exact CAF evaluation near it.

    chi(delta, nu) = sum_n a(n + delta) conj(b[n]) exp(-j 2 pi nu t_n), delta in
    samples, with a fractionally delayed by an FFT phase ramp over the overlap (the
    circular wrap is one sample in N) and t_n in seconds from the midpoint of channel B.

    The B samples used are symmetric about the midpoint of all B samples (as many are
    dropped at one end as the lag forces at the other), so the A samples are centred
    one lag later. With delays and Dopplers drifting linearly over the capture the peak
    is then exactly at D_A(t_mid + tau) - D_B(t_mid) and f_A(t_mid + tau) - f_B(t_mid),
    the definitions of MATH.md section 5; using every overlapping sample would move
    both epochs by tau / 2 for a 2 % gain in samples.
    """

    def __init__(self, a: np.ndarray, b: np.ndarray, fs: float, lag_int: int) -> None:
        skip = max(0, -lag_int, b.size + lag_int - a.size)
        usable = b.size - 2 * skip
        if usable < 32:
            raise ValueError("delay_window leaves fewer than 32 overlapping samples")
        n = _prev_fast_len(usable)
        n0 = skip + (usable - n) // 2
        self.fs = fs
        self.lag_int = lag_int
        self.n = n
        self.a = a[n0 + lag_int : n0 + lag_int + n]
        self.b = b[n0 : n0 + n]
        self.t = (np.arange(n) - 0.5 * (n - 1)) / fs
        self.w = 2.0 * math.pi * sfft.fftfreq(n, 1.0 / fs)  # rad/s
        self.fa = sfft.fft(self.a)
        self.energy_a = float(np.sum(_abs2(self.a)))
        self.energy_b = float(np.sum(_abs2(self.b)))

    def _b_conj(self, nu: float) -> np.ndarray:
        return np.conj(self.b) * np.exp((-2j * math.pi * nu) * self.t)

    def probe(self, deltas: Sequence[float], nu: float) -> np.ndarray:
        """|chi|^2 at each delay offset (samples) for one Doppler (Hz)."""
        z = self.fa * np.conj(sfft.fft(np.conj(self._b_conj(nu))))
        out = np.empty(len(deltas))
        for i, d in enumerate(deltas):
            out[i] = abs(np.sum(z * np.exp(1j * self.w * (d / self.fs)))) ** 2
        return out / float(self.n) ** 2

    def derivatives(self, delta: float, nu: float, with_delay: bool) -> tuple[complex, np.ndarray, np.ndarray]:
        """chi, gradient and Hessian of |chi|^2 in (tau [s], nu [Hz]) at (delta [samples], nu).

        with_delay False leaves the delay row and column of the result at zero.
        """
        cb = self._b_conj(nu)
        a1 = a2 = None
        if with_delay:
            fad = self.fa * np.exp(1j * self.w * (delta / self.fs))
            stack = np.empty((3, self.n), dtype=np.complex128)
            stack[0] = fad
            stack[1] = 1j * self.w * fad
            stack[2] = -(self.w * self.w) * fad
            a0, a1, a2 = sfft.ifft(stack, axis=1)
        elif delta == 0.0:
            a0 = self.a
        else:
            a0 = sfft.ifft(self.fa * np.exp(1j * self.w * (delta / self.fs)))
        p0 = a0 * cb
        tp0 = self.t * p0
        chi = complex(np.sum(p0))
        chi_n = -2j * math.pi * complex(np.sum(tp0))
        chi_nn = -((2.0 * math.pi) ** 2) * complex(np.sum(self.t * tp0))
        g = np.zeros(2)
        hess = np.zeros((2, 2))
        g[1] = 2.0 * (chi_n * chi.conjugate()).real
        hess[1, 1] = 2.0 * (chi_nn * chi.conjugate()).real + 2.0 * abs(chi_n) ** 2
        if a1 is not None and a2 is not None:
            p1 = a1 * cb
            chi_t = complex(np.sum(p1))
            chi_tt = complex(np.sum(a2 * cb))
            chi_tn = -2j * math.pi * complex(np.sum(self.t * p1))
            g[0] = 2.0 * (chi_t * chi.conjugate()).real
            hess[0, 0] = 2.0 * (chi_tt * chi.conjugate()).real + 2.0 * abs(chi_t) ** 2
            hess[0, 1] = hess[1, 0] = 2.0 * (chi_tn * chi.conjugate() + chi_t * chi_n.conjugate()).real
        return chi, g, hess

    def cross_spectrum(self, delta: float, nu: float) -> tuple[np.ndarray, np.ndarray]:
        """Aligned cross-spectrum Y_A conj(Y_B) (peak phase removed) and its frequencies in Hz."""
        fb = sfft.fft(np.conj(self._b_conj(nu)))
        z = self.fa * np.exp(1j * self.w * (delta / self.fs)) * np.conj(fb)
        total = complex(np.sum(z))
        if total != 0.0:
            z = z * (total.conjugate() / abs(total))
        return z, self.w / (2.0 * math.pi)


@dataclass
class _Peak:
    """A refined peak of the exact CAF."""

    lag: float  # array lag, samples (fractional)
    nu: float  # Hz
    power: float  # |chi|^2 (raw units)
    hess: np.ndarray  # d2 |chi|^2 / d(tau [s], nu [Hz])^2
    n_ov: int  # samples in the sum
    energy_a: float
    energy_b: float
    delay_refined: bool  # False when the delay was held (flat cut or no curvature)
    converged: bool  # the last Newton step was negligible
    aligned: _Aligned
    delta: float  # lag - aligned.lag_int, samples

    def rho_tf(self) -> float:
        """Correlation of the delay and Doppler errors from the curvature (0 when undefined)."""
        h_tt, h_tn, h_nn = self.hess[0, 0], self.hess[0, 1], self.hess[1, 1]
        if not (self.delay_refined and h_tt < 0.0 and h_nn < 0.0):
            return 0.0
        return float(min(max(h_tn / math.sqrt(h_tt * h_nn), -1.0), 1.0))


def _refine(
    a: np.ndarray, b: np.ndarray, fs: float, lag0: float, nu0: float, delay_free: bool, lobe: float = 1.0
) -> _Peak:
    """MATH.md section 6 item 2: Newton on the exact CAF from a coarse cell.

    lag0: coarse array lag, samples (integer or half-integer). nu0: coarse Doppler, Hz.
    delay_free: False holds the delay at the nearest integer lag (flat delay cut).
    lobe: width of the delay main lobe, samples (fs / B, at least 1); the coarse cell
    may sit anywhere on the flat top of a wide lobe, so steps scale with it.
    """
    lobe = max(lobe, 1.0)
    lag_int = int(math.floor(lag0 + 0.5))
    al = _Aligned(a, b, fs, lag_int)
    t_int = al.n / fs
    delta, nu = (lag0 - lag_int if delay_free else 0.0), nu0
    if delay_free:
        offs = [delta + lobe * d for d in (-0.5, -0.25, 0.0, 0.25, 0.5)]
        delta = offs[int(np.argmax(al.probe(offs, nu)))]
    delta_start, nu_start = delta, nu
    use_delay = delay_free
    converged = False
    reprobed = False
    chi, g, hess = al.derivatives(delta, nu, use_delay)
    for _ in range(24):
        # Newton step in (samples, cycles over the capture) so the system is well scaled.
        gu, gv = g[0] / fs, g[1] / t_int
        huu, huv, hvv = hess[0, 0] / fs**2, hess[0, 1] / (fs * t_int), hess[1, 1] / t_int**2
        det = huu * hvv - huv * huv
        if use_delay and huu < 0.0 and det > 0.0:
            du = -(hvv * gu - huv * gv) / det
            dv = -(huu * gv - huv * gu) / det
        elif use_delay and huu < 0.0 and hvv < 0.0:
            # Curvature in both, yet singular: a delay-Doppler ridge (correlation +/- 1).
            break
        elif hvv < 0.0:
            if use_delay and not reprobed:
                # Convex in delay here: noise wider than the signal ripples the lobe.
                # Look for the top on a finer grid once before giving the delay up.
                reprobed = True
                offs = [delta + lobe * k / 16.0 for k in range(-4, 5)]
                delta = offs[int(np.argmax(al.probe(offs, nu)))]
                chi, g, hess = al.derivatives(delta, nu, True)
                continue
            if use_delay:
                # No curvature in delay: hold it and finish in Doppler alone.
                use_delay = False
                chi, g, hess = al.derivatives(delta, nu, False)
                continue
            du, dv = 0.0, -g[1] / t_int / hvv
        else:
            break
        # Step limit (0.3 of a main lobe in each direction). A step that is too long
        # is shortened as a whole: clipping its components one by one would turn it
        # away from the ascent direction whenever delay and Doppler are coupled, and
        # the search would then stall on a slope.
        shrink = min(1.0, 0.3 * lobe / abs(du) if du != 0.0 else 1.0, 0.3 / abs(dv) if dv != 0.0 else 1.0)
        if shrink < 1.0:
            du, dv = shrink * du, shrink * dv
            if use_delay and huu < 0.0 and det > 0.0:
                # Better still on the face the full step ran into: the top of the
                # quadratic model with the limited coordinate held at its limit.
                if abs(abs(du) - 0.3 * lobe) <= 1e-12 * lobe:
                    alt = -(gv + huv * du) / hvv
                    if abs(alt) <= 0.3:
                        dv = alt
                else:
                    alt = -(gu + huv * dv) / huu
                    if abs(alt) <= 0.3 * lobe:
                        du = alt
        accepted = False
        for _ in range(8):
            trial = al.derivatives(delta + du, nu + dv / t_int, use_delay)
            if abs(trial[0]) ** 2 >= abs(chi) ** 2 * (1.0 - 1e-12):
                accepted = True
                break
            du, dv = 0.5 * du, 0.5 * dv
        if not accepted:
            converged = abs(du) < 1e-4 and abs(dv) < 1e-4
            break
        delta, nu = delta + du, nu + dv / t_int
        chi, g, hess = trial
        if abs(du) < 1e-7 and abs(dv) < 1e-7:
            converged = True
            break
        if abs(delta - delta_start) > 1.5 * lobe or abs(nu - nu_start) * t_int > 1.5:
            break
    return _Peak(
        lag=lag_int + delta,
        nu=nu,
        power=abs(chi) ** 2,
        hess=hess,
        n_ov=al.n,
        energy_a=al.energy_a,
        energy_b=al.energy_b,
        delay_refined=use_delay,
        converged=converged,
        aligned=al,
        delta=delta,
    )


def _spectral_moments(peak: _Peak, fs: float, noise_bw: float, snr: float) -> tuple[float, float]:
    """RMS bandwidth of the correlated signal and of the incoherent residue, Hz.

    P(f) = Re{Y_A conj(Y_B) exp(j 2 pi f tau) exp(-j arg chi)} after Doppler alignment.
    The spectrum is smoothed over at least 16 bins and only the band where the smoothed
    value is significantly positive (3.5 sigma, widened by the smoothing length) enters
    the moments, so noise-only bins of a narrow signal do not inflate its bandwidth.
    A stretch of that band counts only if it holds a clearly significant value
    (`_SEED_SIGMA`, or half of what a signal filling the band would show): among the
    many noise-only smoothing windows of a narrow signal one passes 3.5 sigma by
    chance every few captures, and far from the centroid it would dominate the
    second moment.
    The variance of the imaginary part, (G_AA G_BB - |G_AB|^2) / 2 per bin, measures
    the incoherent power that drives the delay error.
    Returns (B_rms, B_rms of the incoherent part about the same centroid); B_rms is 0
    when it cannot be measured.
    """
    z, f = peak.aligned.cross_spectrum(peak.delta, peak.nu)
    order = np.argsort(f, kind="stable")
    f = f[order]
    re = z.real[order]
    im2 = z.imag[order] ** 2
    n = f.size
    n_band = max(32, int(round(n * min(noise_bw / fs, 1.0))))
    k_smooth = int(min(max(snr / 16.0, 1.0), 128.0))
    m = max(17, (n_band // k_smooth) | 1)
    mask = np.ones(n, dtype=bool)
    if 2 * m < n_band:
        smooth = ndimage.uniform_filter1d(re, size=m, mode="constant")
        sigma = np.sqrt(np.maximum(ndimage.uniform_filter1d(im2, size=m, mode="constant"), 0.0) / m)
        significant = smooth > 3.5 * sigma
        if np.any(significant):
            mask = ndimage.maximum_filter1d(significant.view(np.uint8), size=m, mode="constant").astype(bool)
            # Hysteresis: keep the stretches seeded by a clearly significant value.
            seed_level = max(_SEED_SIGMA, 0.5 * math.sqrt(2.0 * max(snr, 0.0) / k_smooth))
            seeds = smooth > seed_level * sigma
            labels, n_runs = ndimage.label(mask)
            if n_runs > 1:
                if np.any(seeds):
                    keep = np.unique(labels[seeds & mask])
                else:
                    keep = np.array([labels[int(np.argmax(np.where(mask, smooth, -np.inf)))]])
                mask = np.isin(labels, keep[keep > 0])
    s0 = float(np.sum(re[mask]))
    if s0 <= 0.0:
        return 0.0, 0.0
    f_c = float(np.sum((f * re)[mask])) / s0
    df = f - f_c
    var_sig = float(np.sum((df * df * re)[mask])) / s0
    total_im2 = float(np.sum(im2))
    var_noise = float(np.sum(df * df * im2)) / total_im2 if total_im2 > 1e-24 * s0 * s0 else 0.0
    rms_sig = math.sqrt(var_sig) if var_sig > 0.0 else 0.0
    return rms_sig, math.sqrt(max(var_noise, 0.0))


def _signal_noise_share(peak: _Peak) -> float | None:
    """Share of the signal-times-noise terms in the incoherent power at a peak, 0..1.

    The noise on the surface around a peak is partly signal times noise, which has
    the time-frequency structure of the signal, and partly noise times noise, which
    has none. Their proportion decides how far a delay-Doppler coupled waveform
    spreads the errors (`crlb.sigmas_from_coherence`) and is not determined by the
    coherence. It is measured here from the cross-spectrogram of the aligned
    channels (`_SHARE_BLOCKS` time blocks): the time-frequency covariance of the
    incoherent power ``Im^2`` over that of the signal, the latter from the curvature
    of the peak, ``cov(f, t) = H_tau,nu / (8 pi^2 |chi|^2)``.

    Returns None when the peak has no measurable coupling to compare with.
    """
    al = peak.aligned
    block = al.n // _SHARE_BLOCKS
    cov_signal = peak.hess[0, 1] / (8.0 * math.pi**2 * peak.power) if peak.power > 0.0 else 0.0
    if block < 8 or cov_signal == 0.0:
        return None
    used = block * _SHARE_BLOCKS
    a0 = sfft.ifft(al.fa * np.exp(1j * al.w * (peak.delta / al.fs)))[:used]
    b0 = (al.b * np.exp((2j * math.pi * peak.nu) * al.t))[:used]
    z = sfft.fft(a0.reshape(_SHARE_BLOCKS, block), axis=1) * np.conj(sfft.fft(b0.reshape(_SHARE_BLOCKS, block), axis=1))
    total = complex(np.sum(z))
    if total == 0.0:
        return None
    weight = (z * (total.conjugate() / abs(total))).imag ** 2
    w_sum = float(np.sum(weight))
    if not w_sum > 0.0:
        return None
    f = sfft.fftfreq(block, 1.0 / al.fs)
    t = (np.arange(_SHARE_BLOCKS) + 0.5) * (block / al.fs)
    w_f, w_t = weight.sum(axis=0), weight.sum(axis=1)
    f_mean, t_mean = float(f @ w_f) / w_sum, float(t @ w_t) / w_sum
    cov_noise = float((t - t_mean) @ weight @ (f - f_mean)) / w_sum
    return float(min(max(cov_noise / cov_signal, 0.0), 1.0))


# ------------------------------------------------------------------------- top level


@dataclass
class _Setup:
    a: np.ndarray  # channel A, band-limited, unit mean power
    b: np.ndarray
    fs: float
    noise_bw: float  # B, Hz
    t0: float  # t0_offset_s
    t_int: float  # capture length, s (main-lobe width in Doppler is 1 / t_int)
    thr: float  # detection threshold, linear
    thr_pre: float  # coarse pre-threshold, linear
    shape: _FloorShape
    excl: list[_Excl] = field(default_factory=list)


def _lag_plan(st: _Setup, delay_window: tuple[float, float], name: str) -> tuple[int, int, float, float]:
    """Array lags a search covers: (first lag, number of lags, window start, window end) in samples.

    The surface spans the delay window, a guard band on each side (peaks just outside
    throw sidelobes inside), and at least _STAT_LAGS lags for the noise statistics.
    Raises ValueError when that reaches beyond half the capture.
    """
    fs = st.fs
    lag_lo_f = (delay_window[0] - st.t0) * fs
    lag_hi_f = (delay_window[1] - st.t0) * fs
    lobe = int(math.ceil(fs / st.noise_bw))
    guard = _GUARD_LAGS + 8 * lobe
    l_lo = int(math.floor(lag_lo_f)) - 1 - guard
    l_hi = int(math.ceil(lag_hi_f)) + 1 + guard
    n_search = l_hi - l_lo + 1
    ext = max(0, (max(_STAT_LAGS, 8 * lobe) - n_search + 1) // 2)
    lag0, n_lags = l_lo - ext, n_search + 2 * ext
    limit = min(st.a.size, st.b.size) // 2
    if max(abs(lag0), abs(lag0 + n_lags - 1)) >= limit:
        raise ValueError(
            f"{name} ({delay_window[0]:g} .. {delay_window[1]:g} s with t0_offset_s {st.t0:g}, plus a guard "
            f"of {guard} samples) reaches beyond half the capture length ({limit / fs:g} s)"
        )
    return lag0, n_lags, lag_lo_f, lag_hi_f


def _run_search(st: _Setup, delay_window: tuple[float, float], doppler_window: tuple[float, float], name: str) -> _Search:
    lag0, n_lags, lag_lo_f, lag_hi_f = _lag_plan(st, delay_window, name)
    co = _coarse_surface(st.a, st.b, st.fs, lag0, n_lags, doppler_window[0], doppler_window[1], 0.25 * st.thr_pre)
    return _detect(co, st.shape, st.fs, st.t_int, st.noise_bw, (lag_lo_f, lag_hi_f), doppler_window, st.excl, st.thr_pre)


def _measure_exclusions(
    st: _Setup, points: Sequence[tuple[float, float]], spans: Sequence[tuple[float, float]]
) -> list[_Excl]:
    """Exact delay cut through every predicted self-peak (one FFT pair each).

    The cuts are linear correlations (zero outside the captures) scaled like the coarse
    surface: ``|chi|^2 * ref / overlap`` with ref the length of channel B.
    """
    if not points:
        return []
    a, b, fs = st.a, st.b, st.fs
    na, nb = a.size, b.size
    nfft = int(sfft.next_fast_len(na + nb, real=False))  # no circular wrap at any lag
    fa = sfft.fft(a, n=nfft)
    k = sfft.fftfreq(nfft, 1.0 / nfft)
    t = np.arange(nb) / fs
    reach = max(_STAT_LAGS, 8 * int(math.ceil(fs / st.noise_bw)))
    lo_all = min(int(math.floor((s[0] - st.t0) * fs)) for s in spans) - reach
    hi_all = max(int(math.ceil((s[1] - st.t0) * fs)) for s in spans) + reach
    limit = min(na, nb) // 2  # lags the searches can reach (`_lag_plan`)
    out: list[_Excl] = []
    for tau_x, nu_x in points:
        lag_x = (tau_x - st.t0) * fs
        lo = max(min(lo_all, int(math.floor(lag_x)) - reach), -limit)
        hi = min(max(hi_all, int(math.ceil(lag_x)) + reach), limit)
        if abs(lag_x) >= limit or hi <= lo:
            out.append(_Excl(lag_x, nu_x, 0.0, np.zeros(1), int(round(lag_x))))
            continue
        z = fa * np.conj(sfft.fft(b * np.exp((2j * math.pi * nu_x) * t), n=nfft))
        lags = np.arange(lo, hi + 1)
        cut = _abs2(sfft.ifft(z)[lags % nfft]) * (nb / np.maximum(_overlap(na, nb, lags), 1))
        exact = abs(np.sum(z * np.exp((2j * math.pi * lag_x / nfft) * k))) ** 2 / float(nfft) ** 2
        exact *= nb / max(float(_overlap(na, nb, int(round(lag_x)))), 1.0)
        i0 = int(round(lag_x)) - lo
        near = max(1, int(math.ceil(fs / st.noise_bw)))
        local = cut[max(i0 - near, 0) : i0 + near + 1]
        out.append(_Excl(lag_x, nu_x, max(float(exact), float(local.max()) if local.size else 0.0), cut, lo))
    return out


def _to_db(ratio: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore"):
        db = 10.0 * np.log10(np.asarray(ratio, dtype=np.float64))
    return np.maximum(db, _FLOOR_DB).astype(np.float32)


def _decimate_cut(axis: np.ndarray, db: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    if axis.size <= _CUT_POINTS:
        return axis, db
    group = -(-axis.size // _CUT_POINTS)
    n = (axis.size // group) * group
    return axis[:n].reshape(-1, group).mean(axis=1), db[:n].reshape(-1, group).max(axis=1)


def _display(
    sr: _Search, st: _Setup, lag: float, nu: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray, tuple[np.ndarray, np.ndarray], tuple[np.ndarray, np.ndarray]]:
    """Patch around (lag, nu) and the two cuts through it, all in dB over the noise floor."""
    co = sr.coarse
    n_lags, n_cols = co.power.shape
    row = int(min(max(round(lag - co.lag0), sr.rows[0]), sr.rows[1]))
    col = int(min(max(round((nu - co.nu_c) / co.dnu) - co.bin0, sr.cols[0]), sr.cols[1]))
    half = _PATCH // 2
    r0, r1 = max(row - half, 0), min(row + half, n_lags - 1)
    c0, c1 = max(col - half, 0), min(col + half, n_cols - 1)
    delays = (co.lag0 + np.arange(n_lags)) / st.fs + st.t0
    dopplers = co.nu_c + (co.bin0 + np.arange(n_cols)) * co.dnu
    col_floor = sr.floor.at(dopplers)
    patch = _to_db(co.power[r0 : r1 + 1, c0 : c1 + 1] / col_floor[None, c0 : c1 + 1])
    rs = slice(sr.rows[0], sr.rows[1] + 1)
    cs = slice(sr.cols[0], sr.cols[1] + 1)
    delay_cut = _decimate_cut(delays[rs], _to_db(co.power[rs, col] / col_floor[col]))
    doppler_cut = _decimate_cut(dopplers[cs], _to_db(co.power[row, cs] / col_floor[cs]))
    return patch, delays[r0 : r1 + 1], dopplers[c0 : c1 + 1], delay_cut, doppler_cut


def _refine_candidates(st: _Setup, sr: _Search, limit: int) -> tuple[list[tuple[_Peak, _Cand]], float]:
    """Refine the strongest coarse candidates.

    Returns the distinct admissible peaks, strongest first, and the coarse SNR
    (linear, 0 when none) of the strongest candidate that was discarded because its
    peak lies outside the window or inside a notch, or the search did not settle.
    """
    span = 10.0 ** (-_REFINE_SPAN_DB / 10.0)
    slack_nu = 0.5 / st.t_int
    out: list[tuple[_Peak, _Cand]] = []
    discarded = 0.0
    top: float | None = None
    tried = 0
    for cd in sr.cands:
        if not cd.inside:
            continue
        if len(out) >= limit or tried >= 3 * limit or cd.eff < st.thr_pre or (top is not None and cd.eff < top * span):
            break
        tried += 1
        pk = _refine(st.a, st.b, st.fs, cd.lag, cd.nu, not cd.flat, st.fs / st.noise_bw)
        inside = (
            sr.lag_win[0] - 0.5 <= pk.lag <= sr.lag_win[1] + 0.5
            and sr.nu_win[0] - slack_nu <= pk.nu <= sr.nu_win[1] + slack_nu
        )
        settled = pk.converged or abs(pk.rho_tf()) > _COUPLING_LIMIT
        notched = any(
            bool(_in_lobe(np.float64(pk.lag - lb.lag), np.float64(pk.nu - lb.nu), lb.bw, st.fs, st.t_int, 0.0))
            for lb in sr.notches
        )
        if not (inside and settled) or notched:
            discarded = max(discarded, cd.eff)
            continue
        # Two coarse cells that converged on one peak are one peak.
        duplicate = any(
            bool(_in_lobe(np.float64(pk.lag - q.lag), np.float64(pk.nu - q.nu), min(cd.bw, c2.bw), st.fs, st.t_int, 0.0))
            for q, c2 in out
        )
        if duplicate:
            continue
        if top is None:
            top = cd.eff
        out.append((pk, cd))
    out.sort(key=lambda item: -item[0].power)
    return out, discarded


def caf_estimate(
    ya: np.ndarray,
    yb: np.ndarray,
    fs: float,
    *,
    delay_window: tuple[float, float],
    doppler_window: tuple[float, float],
    signal_bw_hz: float | None = None,
    t0_offset_s: float = 0.0,
    exclude: Sequence[tuple[float, float]] = (),
    mirror_window: tuple[tuple[float, float], tuple[float, float]] | None = None,
    min_snr_db: float = 13.0,
    max_tdoa_sigma_s: float = 5e-4,
) -> CafResult:
    """Estimate TDOA and FDOA of channel A against channel B (MATH.md section 6).

    ya, yb: complex baseband samples of the two channels at the same rate; ``ya[m]``
        at labelled time ``tA + m / fs``, ``yb[n]`` at ``tB + n / fs``. The lengths
        may differ.
    fs: sample rate, Hz.
    delay_window: (min, max) admissible TDOA, true A minus B, seconds.
    doppler_window: (min, max) admissible FDOA, A minus B, hertz.
    signal_bw_hz: occupied bandwidth of the signal, Hz, centred on the capture centre.
        Both channels are filtered to it with one linear-phase FIR (pass band widened
        by twice the largest Doppler magnitude of the windows so a shifted copy is
        not clipped). None: no filter, the correlation bandwidth is ``fs``.
    t0_offset_s: ``tA - tB``, seconds (from the integer nanosecond difference).
    exclude: predicted self-peaks ``(tdoa_s, fdoa_hz)`` to notch: the main lobe, the
        predicted sidelobes of a visible self-peak, and every delay at that Doppler
        when its delay cut is flat.
    mirror_window: optional ``((tdoa_min, tdoa_max), (fdoa_min, fdoa_max))`` of the
        swapped pairing; a distinct peak there not more than 6 dB below the primary
        peak sets ``mirror_ambiguous``.
    min_snr_db: detection threshold on the post-correlation SNR, dB.
    max_tdoa_sigma_s: a larger one-sigma TDOA is reported as unobservable, seconds.

    Returns a `CafResult`. Raises ValueError naming the argument for invalid input.
    The differential delay must drift by less than ``0.5 / B`` and the differential
    Doppler by less than ``1 / T`` over the capture (the caller checks this).
    """
    a = _check_iq(ya, "ya")
    b = _check_iq(yb, "yb")
    fs = _check_scalar(fs, "fs")
    if not (math.isfinite(fs) and fs > 0.0):
        raise ValueError(f"fs must be finite and > 0 (got {fs!r})")
    dwin = _check_window(delay_window, "delay_window")
    nwin = _check_window(doppler_window, "doppler_window")
    t0 = _check_scalar(t0_offset_s, "t0_offset_s")
    if not math.isfinite(t0):
        raise ValueError(f"t0_offset_s must be finite (got {t0_offset_s!r})")
    if signal_bw_hz is not None:
        bw_arg = _check_scalar(signal_bw_hz, "signal_bw_hz")
        if not (math.isfinite(bw_arg) and bw_arg > 0.0):
            raise ValueError(f"signal_bw_hz must be finite and > 0, or None (got {signal_bw_hz!r})")
    if not math.isfinite(_check_scalar(min_snr_db, "min_snr_db")):
        raise ValueError(f"min_snr_db must be finite (got {min_snr_db!r})")
    if not (_check_scalar(max_tdoa_sigma_s, "max_tdoa_sigma_s") > 0.0):
        raise ValueError(f"max_tdoa_sigma_s must be > 0 (got {max_tdoa_sigma_s!r})")
    try:
        points = [(float(p[0]), float(p[1])) for p in exclude]
    except (TypeError, ValueError, IndexError) as exc:
        raise ValueError("exclude must be a sequence of (tdoa_s, fdoa_hz) pairs") from exc
    if any(not (math.isfinite(p[0]) and math.isfinite(p[1])) for p in points):
        raise ValueError("exclude must contain finite (tdoa_s, fdoa_hz) pairs")
    mwin: tuple[tuple[float, float], tuple[float, float]] | None = None
    if mirror_window is not None:
        try:
            m_delay, m_doppler = mirror_window
        except (TypeError, ValueError) as exc:
            raise ValueError("mirror_window must be ((tdoa_min, tdoa_max), (fdoa_min, fdoa_max))") from exc
        mwin = (_check_window(m_delay, "mirror_window delay"), _check_window(m_doppler, "mirror_window doppler"))

    for (lo, hi), name in [(nwin, "doppler_window")] + ([(mwin[1], "mirror_window")] if mwin is not None else []):
        if hi - lo > 0.5 * fs or max(abs(lo), abs(hi)) >= 0.5 * fs:
            raise ValueError(
                f"{name} Doppler range ({lo:g} .. {hi:g} Hz) must lie within +/- fs / 2 and span at most fs / 2"
            )

    # 0. Band limiting (same filter on both channels; its group delay is common and cancels).
    noise_bw = fs
    taps: np.ndarray | None = None
    if signal_bw_hz is not None:
        nu_reach = max(abs(nwin[0]), abs(nwin[1]))
        if mwin is not None:
            nu_reach = max(nu_reach, abs(mwin[1][0]), abs(mwin[1][1]))
        design = _band_filter(fs, float(signal_bw_hz) + 2.0 * nu_reach, max(17, min(a.size, b.size) // 8))
        if design is not None:
            taps, noise_bw = design
            a = ssignal.oaconvolve(a, taps, mode="valid")
            b = ssignal.oaconvolve(b, taps, mode="valid")
    # (Sums of squares rather than BLAS dot products: a threaded BLAS would spin up
    # its pool for nothing here.)
    pa = float(np.sum(_abs2(a))) / a.size
    pb = float(np.sum(_abs2(b))) / b.size
    if not (pa > 0.0 and pb > 0.0):
        raise ValueError("ya / yb have no power inside signal_bw_hz")
    a = a / math.sqrt(pa)
    b = b / math.sqrt(pb)

    min_db = float(min_snr_db)
    thr = 10.0 ** (min_db / 10.0)
    st = _Setup(
        a, b, fs, noise_bw, t0, min(a.size, b.size) / fs, thr, thr * 10.0 ** (-_PRE_DB / 10.0), _FloorShape(taps, fs)
    )
    _lag_plan(st, dwin, "delay_window")
    if mwin is not None:
        _lag_plan(st, mwin[0], "mirror_window")
    spans = [dwin] + ([mwin[0]] if mwin is not None else [])
    st.excl = _measure_exclusions(st, points, spans)

    # 1. Coarse search, 5. notches, distinct peaks; 2. fine search of the strongest.
    sr = _run_search(st, dwin, nwin, "delay_window")
    refined, discarded = _refine_candidates(st, sr, _MAX_CAND)

    def _snr(pk: _Peak, search: _Search) -> float:
        # Peak power over the mean off-peak power for the same number of samples in
        # the sum: the floor of the coarse surface belongs to `ref` products, the
        # refined peak is a sum over pk.n_ov of them.
        return pk.power * (search.coarse.ref / pk.n_ov) / float(search.floor.at(pk.nu))

    def _triple(lag: float, nu: float, snr_lin: float) -> tuple[float, float, float]:
        return (float(lag / fs + t0), float(nu), float(10.0 * math.log10(max(snr_lin, 1e-3))))

    if not refined:
        # No admissible peak reached the pre-threshold: report the strongest cell left.
        inside = [cd for cd in sr.cands if cd.inside]
        rest = [cd for cd in inside if cd.eff < st.thr_pre]
        best = rest[0] if rest else (inside[0] if inside else None)
        if best is None:
            lag_b, nu_b, snr_b = 0.5 * (sr.lag_win[0] + sr.lag_win[1]), 0.5 * (nwin[0] + nwin[1]), 1e-3
        else:
            lag_b, nu_b, snr_b = best.lag, best.nu, max(best.eff, 1e-3)
        patch, d_axis, n_axis, d_cut, n_cut = _display(sr, st, lag_b, nu_b)
        lag_i = int(round(lag_b))
        n_ov = max(int(_overlap(a.size, b.size, lag_i)), 1)
        # A coarse cell is |chi|^2 * ref / n_ov (see `_Coarse`); a and b have unit mean power.
        coh = min(max(snr_b * float(sr.floor.at(nu_b)) / (sr.coarse.ref * n_ov), 0.0), 1.0)
        return CafResult(
            tdoa_s=None, fdoa_hz=None, sigma_tdoa_s=None, sigma_fdoa_hz=None, rho_tf=0.0,
            snr_db=float(10.0 * math.log10(snr_b)), coherence=coh, detected=False,
            mirror_ambiguous=False, candidates=[], rms_bandwidth_hz=None, surface=patch,
            delay_axis_s=d_axis, doppler_axis_hz=n_axis, delay_cut=d_cut, doppler_cut=n_cut,
            reason=REASON_OUTSIDE if discarded >= thr else REASON_BELOW_THRESHOLD,
            noise_bandwidth_hz=noise_bw, integration_s=n_ov / fs,
        )

    main, main_cd = refined[0]
    snr_lin = _snr(main, sr)
    snr_db = float(10.0 * math.log10(max(snr_lin, 1e-3)))
    t_sum = main.n_ov / fs
    coh = float(min(main.power / (main.energy_a * main.energy_b), 1.0))
    patch, d_axis, n_axis, d_cut, n_cut = _display(sr, st, main.lag, main.nu)
    # Refined peaks first (strongest first), then the distinct coarse peaks that were
    # too far below the strongest to be worth refining.
    candidates = [_triple(pk.lag, pk.nu, _snr(pk, sr)) for pk, _ in refined]
    weakest = refined[0][1].eff * 10.0 ** (-_REFINE_SPAN_DB / 10.0)
    candidates += [_triple(cd.lag, cd.nu, cd.snr) for cd in sr.cands if cd.inside and thr <= cd.eff < weakest]
    candidates = [c for c in candidates if c[2] >= min_db][:_MAX_CAND]
    result = CafResult(
        tdoa_s=None, fdoa_hz=None, sigma_tdoa_s=None, sigma_fdoa_hz=None, rho_tf=0.0,
        snr_db=snr_db, coherence=coh, detected=snr_db >= min_db, mirror_ambiguous=False,
        candidates=candidates, rms_bandwidth_hz=None, surface=patch, delay_axis_s=d_axis,
        doppler_axis_hz=n_axis, delay_cut=d_cut, doppler_cut=n_cut, reason=None,
        noise_bandwidth_hz=noise_bw, integration_s=t_sum,
    )
    if not result.detected:
        result.reason = REASON_BELOW_THRESHOLD
        return result

    # 4. Sigmas from the normalised peak, the measured bandwidths and the curvature.
    rms_bw, noise_rms_bw = _spectral_moments(main, fs, noise_bw, snr_lin)
    flat = not main.delay_refined or _excess(snr_lin, main_cd.pedestal) < _PED_FRACTION * thr
    rho_tf = 0.0 if flat else main.rho_tf()
    result.rho_tf = rho_tf
    coh_sigma = coh
    if flat and main_cd.flat and main_cd.ridge_ratio > 0.0:
        # The peak is the strongest cell of a ridge along delay, lifted by noise; the
        # ridge itself carries the signal, so the sigma follows the ridge.
        coh_sigma = coh * main_cd.ridge_ratio
    # Delay-Doppler coupling spreads both errors. By how much depends on how the
    # incoherent power divides into signal-times-noise and noise-times-noise
    # (MATH.md section 6 item 4 covers the first kind alone and is too small with
    # noise in both channels); the division is measured from the data.
    # The curvature is measured on a noisy surface: rho_tf scatters about the
    # coupling of the waveform with variance F^2 / (2 rho), F the ratio of the two
    # RMS bandwidths, so its square is that much too large on average. The sigmas
    # use the coupling left after removing this excess (none for a noise-like
    # signal, whose rho_tf is all scatter).
    f_ratio = max(noise_rms_bw / rms_bw, 1.0) if (rms_bw > 0.0 and noise_rms_bw > 0.0) else 1.0
    rho_sq = min(max(rho_tf * rho_tf - f_ratio * f_ratio / (2.0 * snr_lin), 0.0), 1.0 - 2e-6)
    rho_sigma = math.copysign(math.sqrt(rho_sq), rho_tf)
    share = _signal_noise_share(main) if abs(rho_sigma) >= _SHARE_MIN_RHO else None
    s_t, s_f = sigmas_from_coherence(
        coh_sigma, noise_bw, rms_bw, t_sum, noise_rms_bw_hz=noise_rms_bw if noise_rms_bw > 0.0 else None,
        rho_tf=rho_sigma, signal_noise_share=share,
    )
    # Never below the estimator's own numerical accuracy (noise-free copies give a
    # coherence of 1 and a Stein sigma of 0).
    sigma_t = max(float(s_t), _NUMERIC_FLOOR / fs)
    sigma_f = max(float(s_f), _NUMERIC_FLOOR / t_sum)
    result.rms_bandwidth_hz = None if flat else float(rms_bw)
    result.sigma_fdoa_hz = sigma_f if math.isfinite(sigma_f) else None
    result.sigma_tdoa_s = sigma_t if (math.isfinite(sigma_t) and not flat) else None

    # 6. Mirror window of the swapped pairing.
    if mwin is not None:
        sm = _run_search(st, mwin[0], mwin[1], "mirror_window")
        lobe_bw = min(main_cd.bw, math.sqrt(12.0) * rms_bw) if rms_bw > 0.0 else main_cd.bw
        for pk, _ in _refine_candidates(st, sm, 3)[0]:
            if bool(_in_lobe(np.float64(pk.lag - main.lag), np.float64(pk.nu - main.nu), lobe_bw, fs, st.t_int, 0.0)):
                continue  # the primary peak seen again through an overlapping window
            m_snr = _snr(pk, sm)
            if m_snr < thr:
                continue
            if pk.power >= main.power * 10.0 ** (-_MIRROR_DB / 10.0):
                result.mirror_ambiguous = True
            entry = _triple(pk.lag, pk.nu, m_snr)
            known = any(
                abs(entry[0] - c[0]) < 0.5 / fs and abs(entry[1] - c[1]) < 0.5 / st.t_int for c in result.candidates
            )
            if not known:
                result.candidates = result.candidates[: _MAX_CAND - 1] + [entry]
            break

    # 4 / 8. What can be reported.
    if abs(rho_tf) > _COUPLING_LIMIT:
        result.reason = REASON_COUPLED
        return result
    second = refined[1][0] if len(refined) > 1 else None
    if second is not None and _snr(second, sr) >= thr and second.power >= main.power * 10.0 ** (-_AMBIGUITY_DB / 10.0):
        result.reason = REASON_AMBIGUOUS
        return result
    result.fdoa_hz = float(main.nu)
    if flat or result.sigma_tdoa_s is None or result.sigma_tdoa_s > float(max_tdoa_sigma_s):
        result.reason = REASON_NARROWBAND
        return result
    result.tdoa_s = float(main.lag / fs + t0)
    return result
