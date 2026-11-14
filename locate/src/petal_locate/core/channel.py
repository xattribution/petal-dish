"""Path rendering, the receiver channel filter and thermal noise (MATH.md section 4)."""
from __future__ import annotations

import functools
import math
from collections.abc import Callable
from typing import Any

import numpy as np
import numpy.typing as npt
import scipy.signal

from petal_locate.core.signals import Waveform, key128

__all__ = [
    "DECIMATE_STOPBAND_DB",
    "decimate",
    "decimate_margin",
    "decimation_fir",
    "render_path",
    "thermal_noise",
]

DECIMATE_STOPBAND_DB: float = 80.0
"""Guaranteed attenuation of the decimation filter beyond 0.5 of the output rate, dB."""

_DECIMATE_DESIGN_DB: float = 82.0
"""Kaiser design figure; with 26 output samples of half-length it yields 81 dB or
better beyond 0.5 fs and a passband ripple of 8e-5 up to 0.4 fs for every m."""

_DECIMATE_HALF_TAPS_PER_M: int = 26
"""Half-length of the decimation FIR in output samples (taps = 2 * 26 * m + 1)."""

TimeFn = Callable[[npt.NDArray[np.float64]], npt.ArrayLike]


def _count(value: Any, name: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise ValueError(f"{name} must be an int, got {value!r}")
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value!r}")
    return int(value)


def _sampled(
    fn: TimeFn | None, t_rel: npt.NDArray[np.float64], name: str
) -> npt.NDArray[np.float64]:
    if fn is None:
        return np.zeros(t_rel.size, dtype=np.float64)
    if not callable(fn):
        raise ValueError(f"{name} must be a function of t_rel or None, got {type(fn).__name__}")
    # Called outside the try block: an error raised inside the caller's function (an
    # orbit table out of span, say) propagates as it is, with its own traceback.
    result = fn(t_rel)
    try:
        raw = np.asarray(result)
        if raw.dtype.kind not in "iuf":
            raise TypeError(f"got dtype {raw.dtype}")
        values = np.broadcast_to(raw.astype(np.float64), t_rel.shape)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{name} must return a real scalar or one value per sample ({t_rel.size}): {exc}"
        ) from None
    if not np.all(np.isfinite(values)):
        raise ValueError(f"{name} returned non-finite values")
    return values


def render_path(
    waveform: Waveform,
    base_ns: int,
    n: int,
    fs: float,
    delay_fn: TimeFn | None,
    phase_fn: TimeFn | None,
    amplitude: complex | npt.ArrayLike,
) -> npt.NDArray[np.complex128]:
    """One propagation path of a waveform, sampled at `fs`.

    `y[k] = amplitude * s(base_ns * 1e-9 + t_k - delay_fn(t_k)) * exp(2j pi phase_fn(t_k))`
    with `t_k = k / fs`, k = 0..n-1.

    waveform: the emitter envelope `s` (unit mean power).
    base_ns: labelled time of sample 0, integer UTC nanoseconds.
    n: number of samples (>= 0). fs: sample rate, Hz.
    delay_fn(t_rel) -> seconds: envelope delay at each labelled time (scalar or [n]).
    phase_fn(t_rel) -> cycles: carrier phase at each labelled time (scalar or [n]);
        only its fractional part matters and it is reduced before the exponential.
    amplitude: linear voltage gain (scalar, or one value per sample).
    `None` for delay_fn or phase_fn means zero. Returns complex128[n].

    The callbacks receive the time axis read-only. An exception raised inside one of
    them propagates unchanged; what they return is checked (real, finite, scalar or
    [n]) and reported as a ValueError naming the callback.
    """
    if not callable(getattr(waveform, "evaluate", None)):
        raise ValueError(
            f"waveform must be a Waveform (an object with evaluate), got {type(waveform).__name__}"
        )
    n = _count(n, "n", 0)
    if isinstance(fs, bool) or not isinstance(fs, (int, float, np.integer, np.floating)):
        raise ValueError(f"fs must be a positive sample rate in Hz, got {fs!r}")
    fs = float(fs)
    if not (math.isfinite(fs) and fs > 0.0):
        raise ValueError(f"fs must be a positive sample rate in Hz, got {fs!r}")
    amp = np.asarray(amplitude)
    if amp.dtype.kind not in "iufc" or not np.all(np.isfinite(amp)):
        raise ValueError("amplitude must be finite and numeric")
    if amp.ndim > 1 or (amp.ndim == 1 and amp.size != n):
        raise ValueError(f"amplitude must be a scalar or have n = {n} values")
    t_rel = np.arange(n, dtype=np.float64) / fs
    # Both callbacks and the envelope use this one time axis: a callback that changed
    # it in place would shift the others without a word, so it is handed out read-only.
    t_rel.flags.writeable = False
    delay = _sampled(delay_fn, t_rel, "delay_fn")
    cycles = _sampled(phase_fn, t_rel, "phase_fn")
    envelope = waveform.evaluate(base_ns, t_rel - delay)
    # exp(2j pi x) depends only on frac(x); removing the integer part first is exact
    # and keeps the argument small (phases reach 1e8 cycles on a Ku-band path).
    rotation = np.exp(2j * np.pi * (cycles - np.rint(cycles)))
    return (amp * envelope) * rotation


def decimation_fir(m: int) -> npt.NDArray[np.float64]:
    """Taps of the decimate-by-m filter (read-only array, `52 * m + 1` taps, m >= 2).

    Linear phase (symmetric, odd length, group delay `26 * m` input samples), unity DC
    gain. In units of the output rate fs: passband to 0.4 fs (ripple below 1e-4),
    stopband from 0.5 fs at `DECIMATE_STOPBAND_DB` or better. Kaiser design.
    """
    return _decimation_fir(_count(m, "m", 2))


@functools.lru_cache(maxsize=32)
def _decimation_fir(m: int) -> npt.NDArray[np.float64]:
    half = _DECIMATE_HALF_TAPS_PER_M * m
    n_taps = 2 * half + 1
    beta = 0.1102 * (_DECIMATE_DESIGN_DB - 8.7)
    cutoff = 0.45 / m  # cycles per input sample; midpoint of the 0.4 .. 0.5 transition
    j = np.arange(n_taps) - half
    taps = 2.0 * cutoff * np.sinc(2.0 * cutoff * j) * np.kaiser(n_taps, beta)
    taps /= np.sum(taps)
    taps.flags.writeable = False
    return taps


def decimate_margin(m: int) -> int:
    """Output samples at each end of `decimate(x, m)` touched by the zero extension.

    The filter treats `x` as zero outside the array, so the first and last
    `decimate_margin(m)` output samples carry an edge transient. 0 for m = 1.
    """
    return 0 if _count(m, "m", 1) == 1 else _DECIMATE_HALF_TAPS_PER_M


def decimate(x: npt.ArrayLike, m: int) -> np.ndarray:
    """Receiver channel filter: low-pass and keep every m-th sample (MATH.md section 4).

    x: 1-D real or complex samples at rate `m * fs`. m: integer decimation factor >= 1.
    Returns `ceil(len(x) / m)` samples at rate fs, float64 or complex128; output sample
    i is at the instant of input sample `i * m` (the filter's group delay, an integer
    number of input samples, is removed exactly). The filter is `decimation_fir(m)`:
    passband to 0.4 fs, at least 60 dB down beyond 0.5 fs, so nothing aliases into the
    capture band. `x` is taken as zero outside the array; see `decimate_margin`.
    m = 1 returns a copy of x.
    """
    m = _count(m, "m", 1)
    arr = np.asarray(x)
    if arr.ndim != 1:
        raise ValueError(f"x must be 1-D, got shape {arr.shape}")
    if arr.dtype.kind not in "biufc":
        raise ValueError(f"x must be numeric, got dtype {arr.dtype}")
    arr = arr.astype(np.complex128 if arr.dtype.kind == "c" else np.float64)  # a copy
    if m == 1 or arr.size == 0:
        return arr
    n_out = -(-arr.size // m)
    # upfirdn returns the full convolution sampled at 0, m, 2m, ...; the group delay
    # 26 m is a multiple of m, so dropping 26 outputs removes it exactly.
    keep = slice(_DECIMATE_HALF_TAPS_PER_M, _DECIMATE_HALF_TAPS_PER_M + n_out)
    taps = decimation_fir(m)
    if arr.dtype.kind != "c":
        return np.ascontiguousarray(scipy.signal.upfirdn(taps, arr, up=1, down=m)[keep])
    # Real taps: filtering the two parts separately is the same arithmetic and twice
    # as fast as a complex pass.
    out = np.empty(n_out, dtype=np.complex128)
    out.real = scipy.signal.upfirdn(taps, arr.real, up=1, down=m)[keep]
    out.imag = scipy.signal.upfirdn(taps, arr.imag, up=1, down=m)[keep]
    return out


def thermal_noise(n: int, key_label: str) -> npt.NDArray[np.complex128]:
    """White circular complex Gaussian noise with `E|x|^2 = 1` per sample.

    n: number of samples (>= 0). key_label: text label hashed by `key128`, for example
    `f"thermal|{scenario.seed}|{channel_id}|{t_start_ns}|{fs}|{fc}"`; the same label
    always gives the same samples, and the first samples do not depend on n.
    """
    n = _count(n, "n", 0)
    rng = np.random.Generator(np.random.Philox(key=key128(key_label)))
    x = rng.standard_normal(2 * n)
    return (x[0::2] + 1j * x[1::2]) / math.sqrt(2.0)
