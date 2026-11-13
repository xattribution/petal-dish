"""Deterministic waveforms (MATH.md section 4, "Deterministic waveforms").

A waveform is the complex envelope `s(t)` of an emitter: a pure function of absolute
time and a 128-bit key, so separate processes synthesise identical samples.
`Waveform.evaluate(base_ns, t_rel)` returns `s(base_ns * 1e-9 + t_rel)` without ever
forming that sum in floating point.

Conventions used here that MATH.md leaves to the implementation:

- `noise`: the band-limiting FIR has 4097 taps (Kaiser, 90 dB) and its *stopband*
  edge sits at `bandwidth_hz / 2`, so nothing above -90 dB lies outside the nominal
  band; the flat part is about 0.99 of `bandwidth_hz` wide.
- `psk`: the grid rate is `4 * symbol_rate_hz` (an integer number of samples per
  symbol, so every symbol sits exactly on a grid point); the raw grid is the
  zero-stuffed symbol sequence and the FIR is the root-raised-cosine pulse sampled at
  4 per symbol (4097 taps, plus/minus 512 symbols, Kaiser tapered). Symbol `c[n]`
  (block `n // 4096`, element `n % 4096` of the same Philox block generator, signs of
  the two normals) is centred on absolute time `(n + 512) / symbol_rate_hz`.
- Block index `b` may be negative (times before 1970); the Philox counter word is
  `b mod 2**64`.
- Gating: the gate is on for the first `duty * period` of every period, period phase
  counted from the Unix epoch. `evaluate` has unit mean power while the gate is on
  (mean power `duty` overall).
"""
from __future__ import annotations

import hashlib
import math
import threading
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
import numpy.typing as npt
import scipy.fft
import scipy.special
from numpy.lib.stride_tricks import sliding_window_view

__all__ = [
    "BLOCK",
    "ChirpWaveform",
    "CwWaveform",
    "NoiseWaveform",
    "PskWaveform",
    "Waveform",
    "key128",
    "raw_block",
    "raw_normals",
    "rrc_pulse",
    "waveform_from_spec",
]

BLOCK: int = 4096
"""Grid samples per generator block."""

GRID_OVERSAMPLE: int = 4
"""Grid rate over bandwidth for `noise` (`fs_base_hz = ceil(4 * bandwidth_hz)`)."""

FIR_TAPS: int = BLOCK + 1
"""Length of the band-limiting FIR; at most BLOCK + 1 so only adjacent blocks are needed."""

NOISE_STOPBAND_DB: float = 90.0
"""Design attenuation of the noise band-limiting FIR beyond `bandwidth_hz / 2`."""

PSK_SAMPLES_PER_SYMBOL: int = 4
"""Grid samples per symbol for `psk`."""

PSK_PULSE_BETA: float = 8.0
"""Kaiser taper applied to the 4097-tap root-raised-cosine pulse."""

INTERP_HALF_TAPS: int = 8
"""The interpolator uses 2 * INTERP_HALF_TAPS = 16 grid samples per output sample."""

INTERP_BETA: float = 12.0
"""Kaiser parameter of the windowed-sinc interpolator.

With 16 taps the worst-case error for a component at up to 0.25 cycles per grid sample
(the widest any waveform here occupies: psk with roll-off 1) is -110 dB.
"""

_INTERP_PHASES: int = 2048
"""Tabulated fractional offsets; coefficients are linearly interpolated between them
(table error -140 dB, far below the kernel's own -110 dB)."""

_PASS_SAMPLES: int = 32768
_CHUNK_SAMPLES: int = 8192
_MAX_PASS_BLOCKS: int = 256
_MAX_MASK_BLOCKS: int = 1 << 16
_NS: int = 10**9


# --------------------------------------------------------------------------- keys


def key128(label: str) -> int:
    """128-bit generator key from a text label (process-independent, MATH.md section 4).

    label: for example `f"emitter|{scenario.seed}|{emitter.id}|{waveform.seed}"`.
    Returns an int in [0, 2**128).
    """
    if not isinstance(label, str):
        raise ValueError(f"label must be a str, got {type(label).__name__}")
    return int.from_bytes(hashlib.blake2b(label.encode(), digest_size=16).digest(), "little")


def _check_key(key: int) -> int:
    if isinstance(key, bool) or not isinstance(key, (int, np.integer)):
        raise ValueError(f"key must be an int in [0, 2**128), got {type(key).__name__}")
    key = int(key)
    if not 0 <= key < (1 << 128):
        raise ValueError("key must be in [0, 2**128)")
    return key


def raw_normals(key: int, block: int) -> npt.NDArray[np.float64]:
    """The 8192 standard normals of generator block `block` (dimensionless).

    `numpy.random.Generator(numpy.random.Philox(key=key, counter=[0, b, 0, 0]))
    .standard_normal(8192)` with `b = block mod 2**64`. The block index goes in the
    second counter word: the first is the one the generator increments, and putting
    the index there would make consecutive blocks shifted copies of each other.
    """
    block = _as_int(block, "block", "an integer block index")
    counter = np.array([0, block % (1 << 64), 0, 0], dtype=np.uint64)
    bitgen = np.random.Philox(key=_check_key(key), counter=counter)
    return np.random.Generator(bitgen).standard_normal(2 * BLOCK)


def raw_block(key: int, block: int) -> npt.NDArray[np.complex128]:
    """Raw white complex grid samples `k = 4096 * block + m`, m = 0..4095.

    Sample m is `(x[2m] + 1j x[2m+1]) / sqrt(2)` of `raw_normals(key, block)`:
    circular complex Gaussian, unit variance, dimensionless.
    """
    x = raw_normals(key, block)
    return (x[0::2] + 1j * x[1::2]) / math.sqrt(2.0)


# ------------------------------------------------------------------ small helpers


def _as_int(value: Any, name: str, what: str) -> int:
    """`value` as a Python int; a float, bool or anything else is a ValueError naming it."""
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise ValueError(f"{name} must be {what}, got {type(value).__name__}")
    return int(value)


def _as_ns(base_ns: Any) -> int:
    return _as_int(base_ns, "base_ns", "an integer number of nanoseconds")


def _real_array(value: Any, name: str) -> npt.NDArray[np.float64]:
    """`value` as a float64 array of finite real numbers (any shape).

    Complex, boolean, text or object input is a ValueError naming the argument: numpy
    would otherwise drop an imaginary part with only a warning.
    """
    try:
        arr = np.asarray(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an array of real numbers: {exc}") from None
    if arr.dtype.kind not in "iuf":
        raise ValueError(f"{name} must be real numbers, got dtype {arr.dtype}")
    arr = arr.astype(np.float64, copy=False)
    if not np.all(np.isfinite(arr)):
        raise ValueError(f"{name} must be finite")
    return arr


def _finite_float(value: Any, name: str) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number, got {value!r}") from None
    if not math.isfinite(out):
        raise ValueError(f"{name} must be finite, got {value!r}")
    return out


def _period_ns(period_s: float, name: str) -> int:
    period = _finite_float(period_s, name)
    period_ns = round(period * 1e9)
    if period <= 0.0 or period_ns < 1:
        raise ValueError(f"{name} must be at least 1 ns, got {period_s!r} s")
    return period_ns


def _phase_in_period(
    base_ns: int, period_ns: int, t: npt.NDArray[np.float64]
) -> tuple[npt.NDArray[np.float64], float]:
    """Time since the start of the current period, seconds, in [0, period).

    `u = ((base_ns % period_ns) * 1e-9 + t) mod (period_ns * 1e-9)` (MATH.md section 4).
    Returns (u, period in seconds).
    """
    period = period_ns * 1e-9
    u = np.mod((base_ns % period_ns) * 1e-9 + t, period)
    # A tiny negative argument rounds to `period` itself; keep it at the end of the period.
    np.minimum(u, np.nextafter(period, 0.0), out=u)
    return u, period


def _kaiser_lowpass(n_taps: int, cutoff: float, beta: float) -> npt.NDArray[np.float64]:
    """Kaiser-windowed sinc low-pass, -6 dB point at `cutoff` cycles per sample."""
    j = np.arange(n_taps) - (n_taps - 1) / 2.0
    return 2.0 * cutoff * np.sinc(2.0 * cutoff * j) * np.kaiser(n_taps, beta)


def rrc_pulse(t_sym: npt.ArrayLike, rolloff: float) -> npt.NDArray[np.float64]:
    """Root-raised-cosine pulse at `t_sym` symbol periods from its centre.

    Unit energy (integral of p^2 over t_sym is 1); its spectrum is the square root of
    the raised cosine with roll-off `rolloff` in [0, 1]. Written as the sum of three
    sinc terms, which has no removable singularities to special-case:
    `p = (1-a) sinc((1-a) t) + a [cos(pi (t + 1/4)) sinc(a t + 1/4)
    + cos(pi (t - 1/4)) sinc(a t - 1/4)]`.
    """
    a = _finite_float(rolloff, "rolloff")
    if not 0.0 <= a <= 1.0:
        raise ValueError(f"rolloff must be in [0, 1], got {rolloff!r}")
    t = _real_array(t_sym, "t_sym")
    return (1.0 - a) * np.sinc((1.0 - a) * t) + a * (
        np.cos(np.pi * (t + 0.25)) * np.sinc(a * t + 0.25)
        + np.cos(np.pi * (t - 0.25)) * np.sinc(a * t - 0.25)
    )


def _build_interp_table() -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    """Polyphase table of the Kaiser-windowed-sinc interpolator.

    Row i holds the 16 weights for fractional offset `mu = i / _INTERP_PHASES`; the
    weight of grid sample `k + j` (j = -7..8) is `sinc(j - mu) * kaiser((j - mu) / 8)`.
    Returns (table[phases, 16], difference to the next row) for linear interpolation.
    The kernel is continuous in mu and vanishes at +/- 8, so the interpolated signal is
    a continuous function of time across grid points.
    """
    half = INTERP_HALF_TAPS
    offsets = np.arange(-half + 1, half + 1, dtype=np.float64)
    mu = np.arange(_INTERP_PHASES + 1, dtype=np.float64) / _INTERP_PHASES
    d = offsets[None, :] - mu[:, None]
    arg = INTERP_BETA * np.sqrt(np.maximum(1.0 - (d / half) ** 2, 0.0))
    table = np.sinc(d) * scipy.special.i0(arg) / scipy.special.i0(INTERP_BETA)
    table[0] = offsets == 0.0  # exact grid values at mu = 0 ...
    table[-1] = offsets == 1.0  # ... and continuity into the next grid interval
    base = np.ascontiguousarray(table[:-1])
    step = np.ascontiguousarray(np.diff(table, axis=0))
    base.flags.writeable = False
    step.flags.writeable = False
    return base, step


_INTERP_BASE, _INTERP_STEP = _build_interp_table()


def _interp_weights(mu: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
    """Interpolation weights [n, 16] for fractional offsets `mu` in [0, 1] (grid samples).

    Column c weights grid sample `k + c - 7` when the output instant is `k + mu`.
    """
    p = mu * _INTERP_PHASES
    i = p.astype(np.intp)
    np.minimum(i, _INTERP_PHASES - 1, out=i)
    frac = p - i
    w = _INTERP_STEP[i]
    w *= frac[:, None]
    w += _INTERP_BASE[i]
    return w


# ----------------------------------------------------------------------- base class


class Waveform:
    """Complex envelope `s(t)` of an emitter as a pure function of absolute time.

    Attributes
    ----------
    kind: "cw", "noise", "chirp" or "psk".
    bandwidth_hz: two-sided occupied bandwidth of the (ungated) envelope, Hz. The
        envelope is centred on 0 Hz and lies inside +/- bandwidth_hz / 2.
    rms_bandwidth_hz: `sqrt(integral f^2 S(f) df / integral S(f) df)` of the ungated
        envelope, Hz (`beta = 2 pi * rms_bandwidth_hz` in MATH.md section 8).
    duty: fraction of each gate period the emitter is on (1.0 = not gated).
    gate_period_s: gate period in seconds (the integer-nanosecond value actually
        used), or None when not gated.
    """

    kind: str = ""

    def __init__(self, *, duty: float = 1.0, period_s: float = 0.0) -> None:
        duty_f = _finite_float(duty, "duty")
        if not 0.0 < duty_f <= 1.0:
            raise ValueError(f"duty must be in (0, 1], got {duty!r}")
        self.duty: float = duty_f
        self._gate_period_ns: int | None = None
        if duty_f < 1.0:
            self._gate_period_ns = _period_ns(period_s, "period_s")

    # -- properties ------------------------------------------------------------

    @property
    def bandwidth_hz(self) -> float:
        """Two-sided occupied bandwidth of the ungated envelope, Hz (0 for cw)."""
        raise NotImplementedError

    @property
    def rms_bandwidth_hz(self) -> float:
        """RMS bandwidth of the ungated envelope about its centre, Hz (0 for cw)."""
        raise NotImplementedError

    @property
    def gate_period_s(self) -> float | None:
        """Gate period, seconds, or None when the waveform is not gated."""
        return None if self._gate_period_ns is None else self._gate_period_ns * 1e-9

    # -- evaluation ------------------------------------------------------------

    def evaluate(self, base_ns: int, t_rel: npt.ArrayLike) -> npt.NDArray[np.complex128]:
        """`s(base_ns * 1e-9 + t_rel)`.

        base_ns: absolute time, integer UTC nanoseconds since the Unix epoch.
        t_rel: float64 seconds relative to base_ns; any shape, any order, any sign,
            not necessarily uniform.
        Returns complex128 of the same shape, dimensionless, unit mean power while
        the gate is on. The value depends only on the instant, not on how it is split
        between base_ns and t_rel.
        """
        base = _as_ns(base_ns)
        t = _real_array(t_rel, "t_rel")
        flat = np.ascontiguousarray(t).reshape(-1)
        if self._gate_period_ns is None:
            out = self._envelope(base, flat)
        else:
            on = self._gate_on(base, flat)
            out = np.zeros(flat.size, dtype=np.complex128)
            if on.any():
                out[on] = self._envelope(base, flat[on])
        return out.reshape(t.shape)

    def gate(self, base_ns: int, t_rel: npt.ArrayLike) -> npt.NDArray[np.bool_]:
        """True where the emitter is on at `base_ns * 1e-9 + t_rel` (seconds)."""
        base = _as_ns(base_ns)
        t = _real_array(t_rel, "t_rel")
        if self._gate_period_ns is None:
            return np.ones(t.shape, dtype=bool)
        return self._gate_on(base, t.reshape(-1)).reshape(t.shape)

    def gate_fraction(self, base_ns: int, duration_s: float) -> float:
        """Fraction of `[base_ns, base_ns + duration_s)` during which the emitter is on.

        duration_s: seconds, >= 0 (0 returns the instantaneous state as 0.0 or 1.0).
        Exact for the rectangular gate; 1.0 for an ungated waveform. This is the
        factor by which a snippet's mean power is below the on-power.
        """
        base = _as_ns(base_ns)
        duration = _finite_float(duration_s, "duration_s")
        if duration < 0.0:
            raise ValueError(f"duration_s must be >= 0, got {duration_s!r}")
        if self._gate_period_ns is None:
            return 1.0
        period = self._gate_period_ns * 1e-9
        on = self.duty * period
        u0 = (base % self._gate_period_ns) * 1e-9
        if duration == 0.0:
            return 1.0 if u0 < on else 0.0

        def on_time(x: float) -> float:
            cycles = math.floor(x / period)
            return cycles * on + min(x - cycles * period, on)

        return min(max((on_time(u0 + duration) - on_time(u0)) / duration, 0.0), 1.0)

    def _gate_on(self, base_ns: int, t: npt.NDArray[np.float64]) -> npt.NDArray[np.bool_]:
        assert self._gate_period_ns is not None
        u, period = _phase_in_period(base_ns, self._gate_period_ns, t)
        return u < self.duty * period

    def _envelope(self, base_ns: int, t: npt.NDArray[np.float64]) -> npt.NDArray[np.complex128]:
        """Ungated envelope at `base_ns * 1e-9 + t`; t is 1-D float64 seconds."""
        raise NotImplementedError

    # -- spectrum --------------------------------------------------------------

    def spectral_shape(
        self, freqs_hz: npt.ArrayLike, resolution_hz: float | None = None
    ) -> npt.NDArray[np.float64]:
        """Power spectral density of the ungated envelope, 1/Hz, integrating to 1.

        freqs_hz: baseband offsets from the envelope centre, Hz (any shape).
        resolution_hz: analysis bin width, Hz. When None it is taken from the spacing
            of `freqs_hz` if that is a uniformly spaced 1-D array of at least two
            points (the normal case: PSD bin centres), otherwise no averaging is done.

        With a resolution, each value is the density averaged over a bin of that
        width centred on the frequency, so `sum(shape) * resolution_hz` is exactly
        the fraction of the power inside the covered span (1 when the band is inside
        it), however narrow the signal. Shapes: flat over `bandwidth_hz` for noise and
        chirp, raised cosine (root-raised-cosine squared) for psk. A cw line has no
        density of its own: it is returned as the Hann-window spectral kernel of the
        Welch estimate of MATH.md section 7 (main lobe 2/3, 1/6, 1/6 of the power for
        an on-bin tone, plus its sidelobes), which needs a resolution.

        The simulator's fast path multiplies by the bin width and, for a gated
        waveform, by `gate_fraction`.
        """
        f = _real_array(freqs_hz, "freqs_hz")
        res = self._resolution(f, resolution_hz)
        if res is None:
            return self._density(f)
        return self._binned(f, res)

    @staticmethod
    def _resolution(f: npt.NDArray[np.float64], resolution_hz: float | None) -> float | None:
        if resolution_hz is not None:
            res = _finite_float(resolution_hz, "resolution_hz")
            if res <= 0.0:
                raise ValueError(f"resolution_hz must be > 0, got {resolution_hz!r}")
            return res
        if f.ndim != 1 or f.size < 2:
            return None
        step = float(f[-1] - f[0]) / (f.size - 1)
        if step == 0.0 or not np.allclose(np.diff(f), step, rtol=1e-6, atol=0.0):
            return None
        return abs(step)

    def _binned(self, f: npt.NDArray[np.float64], res: float) -> npt.NDArray[np.float64]:
        return (self._cumulative(f + 0.5 * res) - self._cumulative(f - 0.5 * res)) / res

    def _density(self, f: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
        raise NotImplementedError

    def _cumulative(self, f: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
        """Fraction of the power below baseband offset f (0 to 1)."""
        raise NotImplementedError

    def _gate_repr(self) -> str:
        if self._gate_period_ns is None:
            return ""
        return f", duty={self.duty:g}, period_s={self._gate_period_ns * 1e-9:g}"


def _flat_density(f: npt.NDArray[np.float64], width: float) -> npt.NDArray[np.float64]:
    return np.where(np.abs(f) <= 0.5 * width, 1.0 / width, 0.0)


def _flat_cumulative(f: npt.NDArray[np.float64], width: float) -> npt.NDArray[np.float64]:
    return np.clip(f / width + 0.5, 0.0, 1.0)


# ------------------------------------------------------------------------ cw, chirp


class CwWaveform(Waveform):
    """Unmodulated carrier: `s(t) = 1`."""

    kind = "cw"

    def __init__(self, *, duty: float = 1.0, period_s: float = 0.0) -> None:
        super().__init__(duty=duty, period_s=period_s)

    @property
    def bandwidth_hz(self) -> float:
        return 0.0

    @property
    def rms_bandwidth_hz(self) -> float:
        return 0.0

    def _envelope(self, base_ns: int, t: npt.NDArray[np.float64]) -> npt.NDArray[np.complex128]:
        return np.ones(t.size, dtype=np.complex128)

    def _density(self, f: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
        raise ValueError(
            "freqs_hz: a cw line has no spectral density; pass uniformly spaced "
            "frequencies or resolution_hz"
        )

    def _binned(self, f: npt.NDArray[np.float64], res: float) -> npt.NDArray[np.float64]:
        d = f / res
        amp = np.sinc(d) + 0.5 * np.sinc(d - 1.0) + 0.5 * np.sinc(d + 1.0)
        return (2.0 / 3.0) * amp * amp / res

    def __repr__(self) -> str:
        return f"CwWaveform({self._gate_repr().lstrip(', ')})"


class ChirpWaveform(Waveform):
    """Sawtooth linear FM: frequency sweeps from -bandwidth/2 to +bandwidth/2 each period.

    bandwidth_hz: sweep width, Hz. sweep_period_s: sweep period, seconds (rounded to
    integer nanoseconds). With `u` the time since the start of the current sweep
    (sweeps start at multiples of the period since the Unix epoch) and `P` the period,
    the phase is `pi * bandwidth_hz * u * (u - P) / P` radians: zero at both ends of
    the sweep, so the phase is continuous through the frequency fly-back.
    """

    kind = "chirp"

    def __init__(
        self,
        bandwidth_hz: float,
        sweep_period_s: float = 0.01,
        *,
        duty: float = 1.0,
        period_s: float = 0.0,
    ) -> None:
        super().__init__(duty=duty, period_s=period_s)
        bw = _finite_float(bandwidth_hz, "bandwidth_hz")
        if bw <= 0.0:
            raise ValueError(f"bandwidth_hz must be > 0 for chirp, got {bandwidth_hz!r}")
        self._bandwidth_hz = bw
        self._sweep_period_ns = _period_ns(sweep_period_s, "sweep_period_s")

    @property
    def bandwidth_hz(self) -> float:
        return self._bandwidth_hz

    @property
    def rms_bandwidth_hz(self) -> float:
        return self._bandwidth_hz / math.sqrt(12.0)

    @property
    def sweep_period_s(self) -> float:
        """Sweep period actually used (integer nanoseconds), seconds."""
        return self._sweep_period_ns * 1e-9

    def _envelope(self, base_ns: int, t: npt.NDArray[np.float64]) -> npt.NDArray[np.complex128]:
        u, period = _phase_in_period(base_ns, self._sweep_period_ns, t)
        cycles = (0.5 * self._bandwidth_hz / period) * u * (u - period)
        return np.exp(2j * np.pi * cycles)

    def _density(self, f: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
        return _flat_density(f, self._bandwidth_hz)

    def _cumulative(self, f: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
        return _flat_cumulative(f, self._bandwidth_hz)

    def __repr__(self) -> str:
        return (
            f"ChirpWaveform(bandwidth_hz={self._bandwidth_hz:g}, "
            f"sweep_period_s={self.sweep_period_s:g}{self._gate_repr()})"
        )


# ------------------------------------------------------------------ grid waveforms


class _GridWaveform(Waveform):
    """A waveform defined by band-limited samples on an absolute-time grid.

    Grid sample `k` is at absolute time `k / grid_rate_hz` seconds since the Unix
    epoch. `g[k] = sum_j fir[j] raw[k - j]` (plain causal convolution) with `raw`
    generated in blocks of 4096 from the key; values between grid points come from the
    16-tap Kaiser-windowed-sinc interpolator.

    Filtered blocks are cached per object so that repeated calls for nearby times (the
    other channel of a pair, the next segment of a capture) cost no generation. The
    cache holds at most `cache_blocks` blocks of 64 KiB, least recently used dropped
    first, and nothing farther than `cache_blocks` blocks from the times of the latest
    `evaluate` call, so a long-lived waveform does not keep old captures. It is
    thread-safe and never changes a value.
    """

    def __init__(
        self,
        key: int,
        grid_rate_hz: int,
        fir: npt.NDArray[np.float64],
        *,
        cache_blocks: int,
        duty: float,
        period_s: float,
    ) -> None:
        super().__init__(duty=duty, period_s=period_s)
        if isinstance(cache_blocks, bool) or not isinstance(cache_blocks, (int, np.integer)):
            raise ValueError(f"cache_blocks must be an int, got {cache_blocks!r}")
        if cache_blocks < 0:
            raise ValueError(f"cache_blocks must be >= 0, got {cache_blocks!r}")
        if fir.ndim != 1 or not 1 <= fir.size <= FIR_TAPS:
            raise ValueError(f"fir must have 1 to {FIR_TAPS} taps")
        self.key: int = _check_key(key)
        self._grid_rate_hz: int = int(grid_rate_hz)
        self._fir = np.array(fir, dtype=np.float64)
        self._fir.flags.writeable = False
        self._fir_fft = scipy.fft.fft(self._fir, 2 * BLOCK)
        self._cache_blocks = int(cache_blocks)
        self._cache: OrderedDict[int, npt.NDArray[np.complex128]] = OrderedDict()
        self._lock = threading.Lock()
        # Number of filtered blocks generated so far (cache misses); for diagnostics.
        self.blocks_built: int = 0

    def __getstate__(self) -> dict[str, Any]:
        """State for `copy` and `pickle`: the definition only. The block cache and its
        lock stay behind (a lock cannot be copied), so a copy starts with an empty
        cache of its own and gives the same values."""
        state = self.__dict__.copy()
        del state["_cache"], state["_lock"]
        state["blocks_built"] = 0
        return state

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.__dict__.update(state)
        self._fir.flags.writeable = False
        self._cache = OrderedDict()
        self._lock = threading.Lock()

    @property
    def grid_rate_hz(self) -> int:
        """Grid rate, an integer number of hertz."""
        return self._grid_rate_hz

    @property
    def fir(self) -> npt.NDArray[np.float64]:
        """Band-limiting FIR taps `h[j]` (read-only), `g[k] = sum_j h[j] raw[k - j]`."""
        return self._fir

    # -- blocks ----------------------------------------------------------------

    def _raw_blocks(self, blocks: Sequence[int]) -> dict[int, npt.NDArray[np.complex128]]:
        """Raw (unfiltered) grid blocks, complex128[4096] each, keyed by block index."""
        raise NotImplementedError

    def _build_blocks(self, blocks: Sequence[int]) -> dict[int, npt.NDArray[np.complex128]]:
        """Filtered blocks. Each is computed alone with one fixed-size FFT pair, so its
        values do not depend on which other blocks are requested with it."""
        raw = self._raw_blocks(sorted({b - 1 for b in blocks} | set(blocks)))
        buf = np.empty(2 * BLOCK, dtype=np.complex128)
        out: dict[int, npt.NDArray[np.complex128]] = {}
        for b in blocks:
            buf[:BLOCK] = raw[b - 1]
            buf[BLOCK:] = raw[b]
            # Circular convolution of length 8192: outputs 4096.. only involve taps
            # 0..4096 applied to inputs 0..8191, i.e. the plain convolution.
            y = scipy.fft.ifft(scipy.fft.fft(buf) * self._fir_fft)
            blk = np.ascontiguousarray(y[BLOCK:])
            blk.flags.writeable = False
            out[b] = blk
        return out

    def _get_blocks(self, blocks: Sequence[int]) -> list[npt.NDArray[np.complex128]]:
        found: dict[int, npt.NDArray[np.complex128]] = {}
        with self._lock:
            for b in blocks:
                hit = self._cache.get(b)
                if hit is not None:
                    self._cache.move_to_end(b)
                    found[b] = hit
        missing = [b for b in blocks if b not in found]
        if missing:
            built = self._build_blocks(missing)
            with self._lock:
                self.blocks_built += len(built)
                if self._cache_blocks > 0:
                    for b, blk in built.items():
                        self._cache[b] = blk
                        self._cache.move_to_end(b)
                    while len(self._cache) > self._cache_blocks:
                        self._cache.popitem(last=False)
            found.update(built)
        return [found[b] for b in blocks]

    def grid_samples(self, k_first: int, count: int) -> npt.NDArray[np.complex128]:
        """Band-limited grid samples `g[k_first .. k_first + count)` (dimensionless).

        Sample k is the envelope at absolute time `k / grid_rate_hz` seconds.
        """
        k_first = _as_int(k_first, "k_first", "an integer grid index")
        if isinstance(count, bool) or not isinstance(count, (int, np.integer)) or count < 0:
            raise ValueError(f"count must be a non-negative int, got {count!r}")
        if count == 0:
            return np.empty(0, dtype=np.complex128)
        b0 = k_first // BLOCK
        b1 = (k_first + count - 1) // BLOCK
        out = np.empty(count, dtype=np.complex128)
        for start in range(b0, b1 + 1, _MAX_PASS_BLOCKS):
            ids = list(range(start, min(start + _MAX_PASS_BLOCKS, b1 + 1)))
            cat = np.concatenate(self._get_blocks(ids))
            lo = max(k_first, ids[0] * BLOCK)
            hi = min(k_first + count, (ids[-1] + 1) * BLOCK)
            out[lo - k_first : hi - k_first] = cat[lo - ids[0] * BLOCK : hi - ids[0] * BLOCK]
        return out

    # -- interpolation -----------------------------------------------------------

    def _envelope(self, base_ns: int, t: npt.NDArray[np.float64]) -> npt.NDArray[np.complex128]:
        out = np.empty(t.size, dtype=np.complex128)
        if t.size == 0:
            return out
        rate = self._grid_rate_hz
        # Grid arithmetic of MATH.md section 4: exact integer part from Python ints,
        # only the remainder and the relative time in float64.
        q, r = divmod(base_ns * rate, _NS)
        x = r * 1e-9 + t * rate
        if float(np.max(np.abs(x))) >= 2.0**62:
            raise ValueError("t_rel is too far from base_ns for this waveform's grid rate")
        fl = np.floor(x)
        mu = x - fl
        q_block, q_off = divmod(q, BLOCK)
        # Index of the first of the 16 taps, relative to the start of block q_block.
        first = fl.astype(np.int64)
        first += q_off - (INTERP_HALF_TAPS - 1)
        for lo in range(0, t.size, _PASS_SAMPLES):
            self._interpolate(q_block, first, mu, out, lo, min(lo + _PASS_SAMPLES, t.size))
        self._trim_cache(
            q_block + int(first.min()) // BLOCK,
            q_block + (int(first.max()) + 2 * INTERP_HALF_TAPS - 1) // BLOCK,
        )
        return out

    def _trim_cache(self, lo_block: int, hi_block: int) -> None:
        """Drop cached blocks more than `cache_blocks` away from the range just used."""
        keep_lo = lo_block - self._cache_blocks
        keep_hi = hi_block + self._cache_blocks
        with self._lock:
            for b in [b for b in self._cache if b < keep_lo or b > keep_hi]:
                del self._cache[b]

    def _interpolate(
        self,
        q_block: int,
        first: npt.NDArray[np.int64],
        mu: npt.NDArray[np.float64],
        out: npt.NDArray[np.complex128],
        lo: int,
        hi: int,
    ) -> None:
        """Fill out[lo:hi]. Halves the range until the blocks it touches fit one pass,
        so memory stays bounded however the instants are scattered (a single instant
        touches at most two blocks)."""
        taps = 2 * INTERP_HALF_TAPS
        f = first[lo:hi]
        b_min = int(f.min()) // BLOCK
        span = (int(f.max()) + taps - 1) // BLOCK - b_min + 1
        if span > _MAX_MASK_BLOCKS:
            self._interpolate_halves(q_block, first, mu, out, lo, hi)
            return
        # Blocks holding each sample's first and last tap, relative to b_min.
        b_lo = f // BLOCK - b_min
        b_hi = (f + (taps - 1)) // BLOCK - b_min
        mask = np.zeros(span, dtype=bool)
        mask[b_lo] = True
        mask[b_hi] = True
        needed = np.flatnonzero(mask)
        if needed.size > _MAX_PASS_BLOCKS:
            self._interpolate_halves(q_block, first, mu, out, lo, hi)
            return
        base_block = q_block + b_min
        cat = np.concatenate(self._get_blocks([base_block + int(b) for b in needed]))
        # Position of each sample's first tap inside `cat`. A sample's two blocks are
        # consecutive integers and both are present, hence adjacent in `cat`.
        rank = np.cumsum(mask) - 1
        start = rank[b_lo] * BLOCK + (f - (b_lo + b_min) * BLOCK)
        windows = sliding_window_view(cat, taps)
        mu_part = mu[lo:hi]
        for a in range(0, hi - lo, _CHUNK_SAMPLES):
            sl = slice(a, a + _CHUNK_SAMPLES)
            w = _interp_weights(mu_part[sl])
            out[lo + a : lo + a + w.shape[0]] = np.einsum("ij,ij->i", w, windows[start[sl]])

    def _interpolate_halves(
        self,
        q_block: int,
        first: npt.NDArray[np.int64],
        mu: npt.NDArray[np.float64],
        out: npt.NDArray[np.complex128],
        lo: int,
        hi: int,
    ) -> None:
        if hi - lo < 2:  # cannot happen: one instant needs at most two blocks
            raise RuntimeError("internal error: one instant spans more than one pass of blocks")
        mid = (lo + hi) // 2
        self._interpolate(q_block, first, mu, out, lo, mid)
        self._interpolate(q_block, first, mu, out, mid, hi)


class NoiseWaveform(_GridWaveform):
    """Band-limited circular complex Gaussian noise, flat over `bandwidth_hz`.

    key: 128-bit generator key (`key128(label)`). bandwidth_hz: two-sided bandwidth,
    Hz (at least 1 Hz). cache_blocks: block cache size (64 KiB each); it should cover
    the span of one capture, `4 * bandwidth_hz * duration / 4096` blocks, with room for
    the offset between two paths (a 262144-sample capture needs at most 257; the
    default is 512). The grid rate is `ceil(4 * bandwidth_hz)` Hz; the raw grid is
    `raw_block(key, b)`; the FIR is a 4097-tap Kaiser low-pass normalised to unit
    output power whose stopband (-90 dB) starts at `bandwidth_hz / 2`.
    """

    kind = "noise"

    def __init__(
        self,
        key: int,
        bandwidth_hz: float,
        *,
        duty: float = 1.0,
        period_s: float = 0.0,
        cache_blocks: int = 512,
    ) -> None:
        bw = _finite_float(bandwidth_hz, "bandwidth_hz")
        if bw < 1.0:
            raise ValueError(f"bandwidth_hz must be at least 1 Hz for noise, got {bandwidth_hz!r}")
        rate = math.ceil(GRID_OVERSAMPLE * bw)
        beta = 0.1102 * (NOISE_STOPBAND_DB - 8.7)
        transition = (NOISE_STOPBAND_DB - 7.95) / (14.36 * (FIR_TAPS - 1))  # cycles/sample
        edge = bw / (2.0 * rate)
        fir = _kaiser_lowpass(FIR_TAPS, edge - 0.5 * transition, beta)
        fir /= math.sqrt(float(np.sum(fir * fir)))
        super().__init__(key, rate, fir, cache_blocks=cache_blocks, duty=duty, period_s=period_s)
        self._bandwidth_hz = bw
        # RMS bandwidth of the actual filter response (about 0.995 of the ideal
        # bandwidth_hz / sqrt(12) because the transition lies inside the band).
        n_fft = 1 << 16
        power = np.abs(scipy.fft.rfft(fir, n_fft)) ** 2
        f = np.arange(power.size) * (rate / n_fft)
        self._rms_bandwidth_hz = math.sqrt(float(np.sum(f * f * power) / np.sum(power)))

    @property
    def bandwidth_hz(self) -> float:
        return self._bandwidth_hz

    @property
    def rms_bandwidth_hz(self) -> float:
        return self._rms_bandwidth_hz

    def _raw_blocks(self, blocks: Sequence[int]) -> dict[int, npt.NDArray[np.complex128]]:
        return {b: raw_block(self.key, b) for b in blocks}

    def _density(self, f: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
        return _flat_density(f, self._bandwidth_hz)

    def _cumulative(self, f: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
        return _flat_cumulative(f, self._bandwidth_hz)

    def __repr__(self) -> str:
        return f"NoiseWaveform(bandwidth_hz={self._bandwidth_hz:g}{self._gate_repr()})"


class PskWaveform(_GridWaveform):
    """BPSK / QPSK with root-raised-cosine shaping.

    key: 128-bit generator key. symbol_rate_hz: integer symbols per second.
    order: 2 (BPSK, symbols +/-1) or 4 (QPSK, symbols (+/-1 +/- 1j) / sqrt(2)).
    rolloff: RRC roll-off in [0, 1]. cache_blocks: as for `NoiseWaveform` (one block
    is 1024 symbols).

    `s(t) = sum_n a[n] p(t * symbol_rate_hz - n)` with `p` the (Kaiser tapered,
    +/- 512 symbol) RRC pulse scaled for unit mean power and `a[n] = symbols(n, 1)`:
    symbol n is centred on absolute time `n / symbol_rate_hz` seconds since the Unix
    epoch. Occupied bandwidth `symbol_rate_hz * (1 + rolloff)`.
    """

    kind = "psk"

    def __init__(
        self,
        key: int,
        symbol_rate_hz: int,
        order: int = 4,
        rolloff: float = 0.35,
        *,
        duty: float = 1.0,
        period_s: float = 0.0,
        cache_blocks: int = 512,
    ) -> None:
        rate_f = _finite_float(symbol_rate_hz, "symbol_rate_hz")
        if rate_f < 1.0 or rate_f != round(rate_f):
            raise ValueError(
                f"symbol_rate_hz must be a positive integer number of hertz, got {symbol_rate_hz!r}"
            )
        order_f = _finite_float(order, "order")
        if order_f not in (2.0, 4.0):
            raise ValueError(f"order must be 2 (BPSK) or 4 (QPSK), got {order!r}")
        alpha = _finite_float(rolloff, "rolloff")
        if not 0.0 <= alpha <= 1.0:
            raise ValueError(f"rolloff must be in [0, 1], got {rolloff!r}")
        sps = PSK_SAMPLES_PER_SYMBOL
        half = (FIR_TAPS - 1) // 2
        fir = rrc_pulse((np.arange(FIR_TAPS) - half) / sps, alpha) * np.kaiser(
            FIR_TAPS, PSK_PULSE_BETA
        )
        # Zero-stuffed unit-power symbols: mean output power is sum(h^2) / sps.
        fir *= math.sqrt(sps / float(np.sum(fir * fir)))
        super().__init__(
            key, sps * int(rate_f), fir, cache_blocks=cache_blocks, duty=duty, period_s=period_s
        )
        self._symbol_rate_hz = int(rate_f)
        self._order = int(order_f)
        self._rolloff = alpha
        self._symbol_delay = half // sps  # FIR group delay in symbols (512)

    @property
    def symbol_rate_hz(self) -> int:
        """Symbol rate, an integer number of hertz."""
        return self._symbol_rate_hz

    @property
    def order(self) -> int:
        """Constellation size: 2 (BPSK) or 4 (QPSK)."""
        return self._order

    @property
    def rolloff(self) -> float:
        """Root-raised-cosine roll-off, 0 to 1."""
        return self._rolloff

    @property
    def bandwidth_hz(self) -> float:
        """Occupied bandwidth `symbol_rate_hz * (1 + rolloff)`, Hz."""
        return self._symbol_rate_hz * (1.0 + self._rolloff)

    @property
    def rms_bandwidth_hz(self) -> float:
        """RMS width of the raised-cosine spectrum, Hz:
        `symbol_rate_hz * sqrt(1/12 + rolloff^2 (1/4 - 2/pi^2))`."""
        a = self._rolloff
        return self._symbol_rate_hz * math.sqrt(1.0 / 12.0 + a * a * (0.25 - 2.0 / math.pi**2))

    def _symbol_block(self, block: int) -> npt.NDArray[np.complex128]:
        """Generator symbols c[4096 * block + m]: signs of the block's normals."""
        x = raw_normals(self.key, block)
        re = np.where(x[0::2] >= 0.0, 1.0, -1.0)
        if self._order == 2:
            return re.astype(np.complex128)
        return (re + 1j * np.where(x[1::2] >= 0.0, 1.0, -1.0)) / math.sqrt(2.0)

    def symbols(self, n_first: int, count: int) -> npt.NDArray[np.complex128]:
        """Symbols `a[n_first .. n_first + count)`; a[n] is centred on `n / symbol_rate_hz`
        seconds since the Unix epoch. Unit magnitude, dimensionless."""
        n_first = _as_int(n_first, "n_first", "an integer symbol index")
        if isinstance(count, bool) or not isinstance(count, (int, np.integer)) or count < 0:
            raise ValueError(f"count must be a non-negative int, got {count!r}")
        c_first = n_first - self._symbol_delay
        out = np.empty(count, dtype=np.complex128)
        if count == 0:
            return out
        for b in range(c_first // BLOCK, (c_first + count - 1) // BLOCK + 1):
            blk = self._symbol_block(b)
            lo = max(c_first, b * BLOCK)
            hi = min(c_first + count, (b + 1) * BLOCK)
            out[lo - c_first : hi - c_first] = blk[lo - b * BLOCK : hi - b * BLOCK]
        return out

    def _raw_blocks(self, blocks: Sequence[int]) -> dict[int, npt.NDArray[np.complex128]]:
        sps = PSK_SAMPLES_PER_SYMBOL
        per_block = BLOCK // sps
        symbol_blocks: dict[int, npt.NDArray[np.complex128]] = {}
        out: dict[int, npt.NDArray[np.complex128]] = {}
        for b in blocks:
            sb, part = divmod(b, sps)  # grid block b holds symbols 1024 b .. 1024 b + 1023
            if sb not in symbol_blocks:
                symbol_blocks[sb] = self._symbol_block(sb)
            raw = np.zeros(BLOCK, dtype=np.complex128)
            raw[::sps] = symbol_blocks[sb][part * per_block : (part + 1) * per_block]
            out[b] = raw
        return out

    def _density(self, f: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
        rs = float(self._symbol_rate_hz)
        a = self._rolloff
        x = np.abs(f)
        f1 = 0.5 * (1.0 - a) * rs
        f2 = 0.5 * (1.0 + a) * rs
        if a == 0.0:
            return np.where(x <= f1, 1.0 / rs, 0.0)
        taper = 0.5 * (1.0 + np.cos(np.pi * (np.clip(x, f1, f2) - f1) / (a * rs)))
        return np.where(x <= f1, 1.0, np.where(x < f2, taper, 0.0)) / rs

    def _cumulative(self, f: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
        rs = float(self._symbol_rate_hz)
        a = self._rolloff
        f1 = 0.5 * (1.0 - a) * rs
        f2 = 0.5 * (1.0 + a) * rs
        x = np.minimum(np.abs(f), f2)
        y = np.maximum(x - f1, 0.0)  # distance into the roll-off region
        half = np.minimum(x, f1) + 0.5 * y
        if a > 0.0:
            half = half + 0.5 * (a * rs / np.pi) * np.sin(np.pi * y / (a * rs))
        return 0.5 + np.sign(f) * half / rs

    def __repr__(self) -> str:
        return (
            f"PskWaveform(symbol_rate_hz={self._symbol_rate_hz}, order={self._order}, "
            f"rolloff={self._rolloff:g}{self._gate_repr()})"
        )


# -------------------------------------------------------------------------- factory

_SPEC_DEFAULTS: dict[str, Any] = {
    "bandwidth_hz": 0.0,
    "sweep_period_s": 0.01,
    "symbol_rate_hz": 0,
    "order": 4,
    "rolloff": 0.35,
    "duty": 1.0,
    "period_s": 0.0,
}


def _spec_field(spec: Any, name: str) -> Any:
    if isinstance(spec, Mapping):
        value = spec.get(name)
    else:
        value = getattr(spec, name, None)
    if value is None:
        if name not in _SPEC_DEFAULTS:
            raise ValueError(f"waveform spec has no field {name!r}")
        return _SPEC_DEFAULTS[name]
    return value


def waveform_from_spec(spec: Any, key_label: str) -> Waveform:
    """Build the waveform described by a `WaveformSpec` (ARCHITECTURE.md section 4).

    spec: an object or a mapping with `type` ("cw", "noise", "chirp" or "psk") and,
        optionally, `bandwidth_hz` (Hz), `sweep_period_s` (s, chirp), `symbol_rate_hz`
        (integer Hz, psk), `order` (2 or 4, psk), `rolloff` (psk), `duty` and
        `period_s` (s, gating). Missing or None fields take the WaveformSpec defaults.
        The `seed` field is not read here: it is part of `key_label`.
    key_label: text label hashed by `key128`, for example
        `f"emitter|{scenario.seed}|{emitter.id}|{waveform.seed}"`.

    Per type: `cw` ignores `bandwidth_hz`. `noise` and `chirp` need `bandwidth_hz > 0`.
    `psk` uses `symbol_rate_hz` when it is non-zero (then a non-zero `bandwidth_hz`
    must be at least `symbol_rate_hz * (1 + rolloff)`), otherwise the largest integer
    symbol rate that fits `bandwidth_hz`: `floor(bandwidth_hz / (1 + rolloff))`.
    """
    key = key128(key_label)
    kind = _spec_field(spec, "type")
    kind = getattr(kind, "value", kind)
    if not isinstance(kind, str) or kind not in ("cw", "noise", "chirp", "psk"):
        raise ValueError(f"type must be one of cw, noise, chirp, psk; got {kind!r}")
    bandwidth = _finite_float(_spec_field(spec, "bandwidth_hz"), "bandwidth_hz")
    if bandwidth < 0.0:
        raise ValueError(f"bandwidth_hz must be >= 0, got {bandwidth!r}")
    duty = _spec_field(spec, "duty")
    period_s = _spec_field(spec, "period_s")
    if kind == "cw":
        return CwWaveform(duty=duty, period_s=period_s)
    if kind == "chirp":
        return ChirpWaveform(
            bandwidth, _spec_field(spec, "sweep_period_s"), duty=duty, period_s=period_s
        )
    if kind == "noise":
        return NoiseWaveform(key, bandwidth, duty=duty, period_s=period_s)
    rolloff = _finite_float(_spec_field(spec, "rolloff"), "rolloff")
    if not 0.0 <= rolloff <= 1.0:
        raise ValueError(f"rolloff must be in [0, 1], got {rolloff!r}")
    symbol_rate = _finite_float(_spec_field(spec, "symbol_rate_hz"), "symbol_rate_hz")
    if symbol_rate < 0.0:
        raise ValueError(f"symbol_rate_hz must be >= 0, got {symbol_rate!r}")
    if symbol_rate > 0.0:
        if symbol_rate != round(symbol_rate):
            raise ValueError(
                f"symbol_rate_hz must be an integer number of hertz, got {symbol_rate!r}"
            )
        occupied = symbol_rate * (1.0 + rolloff)
        if 0.0 < bandwidth < occupied * (1.0 - 1e-9):
            raise ValueError(
                f"bandwidth_hz={bandwidth!r} is smaller than symbol_rate_hz * (1 + rolloff) "
                f"= {occupied!r}"
            )
        rate = int(round(symbol_rate))
    else:
        if bandwidth <= 0.0:
            raise ValueError("psk needs bandwidth_hz > 0 or symbol_rate_hz > 0")
        rate = math.floor(bandwidth / (1.0 + rolloff) + 1e-6)
        if rate < 1:
            raise ValueError(
                f"bandwidth_hz={bandwidth!r} is too small for a 1 Hz symbol rate at "
                f"rolloff {rolloff!r}"
            )
    return PskWaveform(
        key, rate, _spec_field(spec, "order"), rolloff, duty=duty, period_s=period_s
    )
