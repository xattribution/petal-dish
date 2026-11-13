"""Detection on PSD frames and waveform classification (MATH.md section 7).

Two independent pieces of pure signal processing:

- `SignalTracker` turns the PSD frames of one monitor window into tracked detections:
  learned per-bin baseline, runs above threshold, M-of-N confirmation, release,
  stable track ids, power / SNR / C/N0 and the authorised-carrier rule.
- `classify` labels a short IQ capture as `cw`, `swept`, `noise`, `modulated`,
  `pulsed` or `unknown`.

Conventions shared by both:

- A PSD frame is `float32[nfft]` in dB, fft-shifted, in `welch_psd_db` units
  (unit-variance white noise reads 0 dB in every bin). Bin `k` is centred on
  `center_hz + (k - nfft // 2) * span_hz / nfft` and is `bin_hz = span_hz / nfft` wide.
- Frequencies handed to and returned by the tracker are downlink (RF) hertz.
- Absolute time is integer nanoseconds.

Choices MATH.md leaves to the implementation (each is covered by a test):

- *History.* One frame is kept every `max(baseline_tau_s / 120, 1 / frame_rate_hz)`
  seconds, per bin, in a ring of at most 120 entries. A bin "in detection" (inside the
  extent of a live track, plus one skirt bin on each side, or above threshold in the
  kept frame) neither gains nor loses entries, so its baseline is literally frozen.
  The history of a bin "spans" `(entries - 1) * keep interval`. Carrier bins are
  tested once theirs spans 30 s, or half of `baseline_tau_s` when that is shorter.
- *Track extent.* A track's extent is the hull of the runs associated with it over
  the last `confirm_n` frames, which keeps `f_lo_hz` / `f_hi_hz` and the frozen bins
  steady when single bins dip under the threshold. Power, SNR and C/N0 are measured
  over that extent on every frame in which the track is seen and held otherwise.
- *C/N0.* The sum of MATH.md runs over the extent plus one bin on each side: a Hann
  window puts a third of a bin-centred tone's power in the two adjacent bins, which
  stay under the threshold for a weak tone.
- *Threshold and frame statistics.* The tracker is not told `navg`. `threshold_db`
  above a median baseline is exceeded by a noise bin with probability 3.5e-7 at
  `navg = 8` but 1.0 % at `navg = 2` and 6.3 % at `navg = 1` (an explicit monitor
  may set any `navg`; a derived one drops below 8 only for spans under 1024 Hz),
  where a literal implementation of MATH.md fills the window with false tracks.
  The threshold in force (`effective_threshold_db`) is therefore `threshold_db` or,
  when larger, the level a noise bin exceeds with probability `NOISE_EXCEEDANCE`,
  for the number of averages read from the bin-to-bin scatter of the frames
  themselves: 6.4 dB for 4 averages, 8.5 dB for 2, 11.2 dB for 1, and below 6 dB
  (so not in force with the default `threshold_db`) from 5 averages up. While a bin's history is young (60 to 600 s
  after start-up) its median baseline scatters enough that `navg <= 2` frames can
  still produce the odd false track; none was seen after that.
- *Floor F.* Like the history, F leaves out bins in detection (inside a track, or
  more than the threshold above the previous frame's baseline) and is held when
  too few usable bins remain (16, or half the guard bins when there are fewer than
  32), so an emitter that appears during start-up does not lift its own prior. The
  guard median is used whenever there are at least `MIN_GUARD_BINS` guard bins (a
  derived `nfft = 64` monitor has 13).
- *Floor without guard bins.* The 10th percentile of a frame sits 2.2 dB under its
  median for `navg = 8` (8.2 dB for `navg = 1`), which would lower the start-up
  threshold by that much. The percentile is therefore taken of `psd - R_k` and raised
  by the percentile-to-median offset of the frame's own fluctuation statistics.
- *A signal on at start-up.* A signal present from the first frame can cover most of
  the floor bins (more than half the guard bins, or nine tenths of a window without
  them) and lift the guard median or the 10th percentile with it, so that it would be
  learned as noise. The estimate therefore steps down to a lower quantile (0.25,
  0.10, 0.04, 0.015, each raised by its own offset to the median) whenever that one
  lies lower by more than `FLOOR_SIGNIFICANCE` standard errors, and only when at
  least `FLOOR_LEAST_BINS` bins lie below it: a signal is detected at once as long as
  it leaves free the bins below the lowest quantile tried (1.5 % of 1024 bins, at
  least `FLOOR_LEAST_BINS`). One that fills the whole window cannot be
  told from noise inside it; `floor_db` lets the caller compare the window with the
  receiver's other windows (the server flags it as "floor raised").
- *Merging and splitting tracks.* Unconfirmed fragments joined by one run merge at
  once (with the nearest confirmed track, when the run touches several). Two
  confirmed tracks merge only after sharing a run in `confirm_m` consecutive frames
  (a one-frame broadband transient does not fuse them), under the lower id; until
  then each takes the part of the shared run on its side of the midpoint between
  them, so the extents of two tracks never share a bin. A track is cut at a stretch
  inside its extent that has had no bin above threshold for `release_frames +
  confirm_n` frames when the stretch is wider than `4 * (merge_bins + 1)` bins, or
  wider than `merge_bins` and back at its baseline (mean power of the last
  `confirm_n` frames within `threshold_db / 4` of it): two signals 3 to 12 bins
  apart would otherwise stay fused for good after anything bridged them for
  `confirm_m` frames. Each piece takes back the id it had before,
  and a piece that never had one starts again as an unconfirmed track. When the
  signal a merged track is named after goes away and one it absorbed remains, the
  track carries on under the id and first-seen time of the one that remains.
- *Authorised carriers.* Carrier bins are the bins inside the carrier's range widened
  by two bins (the Hann main lobe). The power compared with `power_anomaly_db` is the
  mean over the bins the track shares with the carrier and over the last `confirm_n`
  frames, relative to the baseline of those bins: the bins of a single frame's run
  are, by selection, already `threshold_db` above it. The baseline is a median and
  that power a mean, so the baseline is first raised by the mean-to-median ratio
  of the frames' fluctuation (from the same scatter estimate as the threshold:
  0.18 dB at `navg = 8`, 1.6 dB at `navg = 1`); otherwise an unchanged carrier
  would already read that much "above" its baseline. "More than" is decided with a
  tolerance of 1e-5 dB (the resolution of a float32 frame), so a step of exactly
  `power_anomaly_db` in noise-free frames is not an anomaly. The anomaly clears after
  `release_frames` consecutive frames at or below `power_anomaly_db`.
"""
from __future__ import annotations

import math
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import numpy.typing as npt
import scipy.fft
import scipy.special
from numpy.lib.stride_tricks import sliding_window_view

__all__ = [
    "CLASS_LABELS",
    "Classification",
    "Detection",
    "SignalTracker",
    "classify",
]

# --------------------------------------------------------------------------- tracker

HISTORY_FRAMES: int = 120
"""Most history entries kept per bin."""

FLOOR_SMOOTH_FRAMES: int = 5
"""Frames over which the start-up floor estimate F is averaged."""

PRIOR_SPAN_FRACTION: float = 0.1
"""The prior is used until a bin's history spans this fraction of baseline_tau_s."""

CARRIER_LEARN_S: float = 30.0
"""Carrier bins are not tested until their own history spans this many seconds."""

FLOOR_PERCENTILE: float = 0.10
"""Quantile of the frame used as the floor when there are no guard bins."""

FLOOR_LOWER_QUANTILES: tuple[float, ...] = (0.25, 0.10, 0.04, 0.015)
"""Lower quantiles the floor estimate may step down to when a signal lifts the usual one."""

FLOOR_LEAST_BINS: int = 8
"""A lower quantile is tried only when at least this many floor bins lie below it."""

FLOOR_SIGNIFICANCE: float = 4.0
"""A lower quantile replaces the floor estimate when it is lower by this many standard errors."""

FLOOR_REFINE_STEPS: int = 3
"""Re-estimates of a floor a signal lifted, each leaving out bins more than the threshold above."""

MIN_FLOOR_BINS: int = 16
"""Fewest bins a floor estimate may rest on (bins free of tracks; fewer with few guard bins)."""

MIN_GUARD_BINS: int = 8
"""Fewest guard bins whose median is taken as the floor; below this, all bins are used."""

NOISE_EXCEEDANCE: float = 1e-4
"""Largest probability with which a noise bin may exceed the threshold in force."""

SCATTER_FRAMES: int = 128
"""Frames over which the bin-to-bin scatter that sets the least threshold is averaged."""

MIN_SCATTER_PAIRS: int = 8
"""Fewest pairs of bins a frame's scatter estimate may rest on."""

EMPTY_FRACTION: float = 0.25
"""A quiet stretch is back at its baseline when within this fraction of threshold_db of it."""

SKIRT_BINS: int = 1
"""Bins on each side of a track's extent that belong to its Hann main lobe."""

CARRIER_PAD_BINS: int = 2
"""Bins added on each side of a configured carrier's range (Hann main-lobe half-width)."""

SPLIT_GAP_RUNS: int = 4
"""A track is cut at a long-quiet stretch wider than this many times merge_bins + 1."""

_LAG = 3  # bins apart at which Hann-windowed periodogram bins are independent
_DB_TOL = 1e-5  # dB; a float32 frame resolves 2e-6 dB at 20 dB and 8e-6 dB at 100 dB
_TINY = 1e-30
_DB_TO_NEPER = math.log(10.0) / 10.0  # 10 ** (x / 10) == exp(_DB_TO_NEPER * x)


@dataclass
class Detection:
    """One confirmed, unreleased signal in a monitor window.

    track_id: identifier, constant for the life of the track and never reused.
    f_lo_hz, f_hi_hz: lower edge of the first bin and upper edge of the last bin of the
        track's extent, downlink Hz.
    power_db: 10 log10 of the mean linear power per bin over the extent, PSD dB units.
    snr_db: power_db minus 10 log10 of the mean linear baseline over the extent, dB.
    cn0_dbhz: carrier power over noise density, dB-Hz (MATH.md section 7).
    first_seen_ns, last_seen_ns: time of the first and of the latest frame in which the
        track was above threshold, integer ns.
    authorized_carrier_id: id of the configured carrier the extent mostly lies on, or None.
    anomaly: True when an authorised carrier is more than power_anomaly_db above its
        baseline (always False without authorized_carrier_id).
    """

    track_id: int
    f_lo_hz: float
    f_hi_hz: float
    power_db: float
    snr_db: float
    cn0_dbhz: float
    first_seen_ns: int
    last_seen_ns: int
    authorized_carrier_id: str | None
    anomaly: bool


@dataclass(slots=True)
class _Track:
    lo: int
    hi: int
    first_seen_ns: int
    last_seen_ns: int
    hits: deque[bool]
    extents: deque[tuple[int, int] | None]
    misses: int = 0
    confirmed: bool = False
    track_id: int = 0
    fuse: int = 0
    power_db: float = 0.0
    snr_db: float = 0.0
    cn0_dbhz: float = 0.0
    carrier: int = -1
    anomaly: bool = False
    quiet: int = 0
    # Undoing merges: the extent this track had on its own, and (id, extent,
    # first_seen_ns) of every confirmed track merged into it.
    home: tuple[int, int] = (0, 0)
    own_first_seen_ns: int = 0
    absorbed: list[tuple[int, tuple[int, int], int]] = field(default_factory=list)


def _cfg_number(cfg: Any, name: str, *, minimum: float, integer: bool = False, strict: bool = False) -> Any:
    """Read `cfg.<name>` and check it; raises ValueError naming the field."""
    try:
        value = getattr(cfg, name)
    except AttributeError:
        raise ValueError(f"cfg.{name} is missing") from None
    if isinstance(value, bool) or not isinstance(value, (int, float, np.integer, np.floating)):
        raise ValueError(f"cfg.{name} must be a number (got {value!r})")
    if integer:
        if int(value) != value:
            raise ValueError(f"cfg.{name} must be an integer (got {value!r})")
        value = int(value)
    else:
        value = float(value)
        if not math.isfinite(value):
            raise ValueError(f"cfg.{name} must be finite (got {value!r})")
    if value < minimum or (strict and value == minimum):
        bound = ">" if strict else ">="
        raise ValueError(f"cfg.{name} must be {bound} {minimum} (got {value!r})")
    return value


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, np.integer, np.floating)):
        raise ValueError(f"{name} must be a number (got {value!r})")
    out = float(value)
    if not math.isfinite(out):
        raise ValueError(f"{name} must be finite (got {value!r})")
    return out


def _runs(mask: npt.NDArray[np.bool_], max_gap: int) -> tuple[npt.NDArray[np.intp], npt.NDArray[np.intp]]:
    """Runs of True in `mask`, joined across gaps of at most `max_gap` False bins.

    Returns (first, last) index arrays, both inclusive, in increasing order.
    """
    edges = np.diff(np.concatenate(([0], mask.view(np.int8), [0])))
    first = np.flatnonzero(edges == 1)
    last = np.flatnonzero(edges == -1) - 1
    if first.size > 1 and max_gap > 0:
        split = (first[1:] - last[:-1] - 1) > max_gap
        first = first[np.concatenate(([True], split))]
        last = last[np.concatenate((split, [True]))]
    return first, last


def _fluctuation_table() -> tuple[
    npt.NDArray[np.float64], npt.NDArray[np.float64], npt.NDArray[np.float64], npt.NDArray[np.float64]
]:
    """Noise statistics of a PSD frame as functions of its bin-to-bin scatter.

    For bins distributed as Gamma(n) / n (n = navg of the Welch estimate) the dB
    difference of two independent bins is 10 log10 of an F(2n, 2n) variate, whose
    median absolute value is `10 log10(b / (1 - b))` with `b` the 0.75 quantile of
    Beta(n, n). The same n gives the distance from the FLOOR_PERCENTILE quantile to
    the median, `10 log10(Q(0.5) / Q(FLOOR_PERCENTILE))`, the distance from the
    median to the level a bin exceeds with probability NOISE_EXCEEDANCE,
    `10 log10(Q(1 - NOISE_EXCEEDANCE) / Q(0.5))`, and the mean over the median,
    `10 log10(n / Q(0.5))`. Returns (scatter_db, offset_db, least_threshold_db,
    mean_over_median_db), all in dB, scatter increasing, for n from 512 down to 0.5.
    """
    n = np.geomspace(512.0, 0.5, 81)
    b = scipy.special.betaincinv(n, n, 0.75)
    scatter = 10.0 * np.log10(b / (1.0 - b))
    median = scipy.special.gammaincinv(n, 0.5)
    offset = 10.0 * np.log10(median / scipy.special.gammaincinv(n, FLOOR_PERCENTILE))
    least = 10.0 * np.log10(scipy.special.gammainccinv(n, NOISE_EXCEEDANCE) / median)
    mean_over_median = 10.0 * np.log10(n / median)
    return scatter, offset, least, mean_over_median


def _quantile_table(
    quantiles: Sequence[float],
) -> dict[float, tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]]:
    """Per quantile p, on the n grid of `_fluctuation_table`: the distance from the
    p-quantile of a noise bin's dB value to its median, `10 log10(Q(0.5) / Q(p))`, and
    the density of the dB value at the p-quantile (per dB), which sets the standard
    error `sqrt(p (1 - p) / N) / density` of a p-quantile taken over N bins."""
    n = np.geomspace(512.0, 0.5, 81)
    median = scipy.special.gammaincinv(n, 0.5)
    out: dict[float, tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]] = {}
    for p in quantiles:
        q = scipy.special.gammaincinv(n, p)  # Gamma(n, 1); the dB density is scale-free
        log_pdf = (n - 1.0) * np.log(q) - q - scipy.special.gammaln(n)
        density = np.exp(log_pdf) * q * math.log(10.0) / 10.0
        out[float(p)] = (10.0 * np.log10(median / q), density)
    return out


class SignalTracker:
    """Detects and tracks signals in the PSD frames of one monitor window.

    center_hz, span_hz: centre and width of the window, downlink Hz.
    nfft: bins per frame (>= 16).
    frame_rate_hz: nominal frames per second (sets the history keep interval).
    cfg: object read by attribute: threshold_db (dB above the baseline, > 0; raised
        for frames of fewer than five averages, see `effective_threshold_db`),
        confirm_m, confirm_n (frames, 1 <= m <= n), release_frames (>= 1),
        merge_bins (>= 0), baseline_tau_s (s, at least 10 frame periods),
        power_anomaly_db (dB, >= 0). Values are read once, here.
    passbands: (f_lo_hz, f_hi_hz, relay_rise_db) of every transponder downlink
        passband the window can see; relay_rise_db = 10 log10(1 + N0_relay / N0_site).
        Overlapping passbands add their relayed noise linearly.
    carriers: (carrier_id, f_lo_hz, f_hi_hz) of the authorised carriers, downlink Hz.

    Invalid arguments raise ValueError naming the argument.
    """

    def __init__(
        self,
        center_hz: float,
        span_hz: float,
        nfft: int,
        frame_rate_hz: float,
        cfg: Any,
        passbands: Sequence[tuple[float, float, float]],
        carriers: Sequence[tuple[str, float, float]],
    ) -> None:
        self.center_hz = _finite(center_hz, "center_hz")
        self.span_hz = _finite(span_hz, "span_hz")
        if self.span_hz <= 0.0:
            raise ValueError(f"span_hz must be > 0 (got {span_hz!r})")
        if isinstance(nfft, bool) or not isinstance(nfft, (int, np.integer)) or nfft < 16:
            raise ValueError(f"nfft must be an integer >= 16 (got {nfft!r})")
        self.nfft = int(nfft)
        self.frame_rate_hz = _finite(frame_rate_hz, "frame_rate_hz")
        if self.frame_rate_hz <= 0.0:
            raise ValueError(f"frame_rate_hz must be > 0 (got {frame_rate_hz!r})")
        self.bin_hz = self.span_hz / self.nfft

        self._threshold_db: float = _cfg_number(cfg, "threshold_db", minimum=0.0, strict=True)
        self._confirm_m: int = _cfg_number(cfg, "confirm_m", minimum=1, integer=True)
        self._confirm_n: int = _cfg_number(cfg, "confirm_n", minimum=1, integer=True)
        if self._confirm_m > self._confirm_n:
            raise ValueError(
                f"cfg.confirm_m must be <= cfg.confirm_n (got {self._confirm_m} > {self._confirm_n})"
            )
        self._release_frames: int = _cfg_number(cfg, "release_frames", minimum=1, integer=True)
        self._merge_bins: int = _cfg_number(cfg, "merge_bins", minimum=0, integer=True)
        tau = _cfg_number(cfg, "baseline_tau_s", minimum=0.0, strict=True)
        if tau * self.frame_rate_hz < 10.0:
            raise ValueError(
                f"cfg.baseline_tau_s must span at least 10 frame periods (got {tau!r} s at {self.frame_rate_hz!r} Hz)"
            )
        self._power_anomaly_db: float = _cfg_number(cfg, "power_anomaly_db", minimum=0.0)

        # history schedule
        keep_s = max(tau / HISTORY_FRAMES, 1.0 / self.frame_rate_hz)
        self._keep_ns = int(round(keep_s * 1e9))
        self._keep_tol_ns = int(round(0.25e9 / self.frame_rate_hz))
        self._capacity = int(min(HISTORY_FRAMES, max(2, round(tau / keep_s))))
        learn_entries = min(self._capacity, int(math.ceil(PRIOR_SPAN_FRACTION * tau / keep_s - 1e-9)) + 1)
        carrier_span_s = min(CARRIER_LEARN_S, 0.5 * tau)
        carrier_entries = min(self._capacity, int(math.ceil(carrier_span_s / keep_s - 1e-9)) + 1)

        k = np.arange(self.nfft)
        freq = self.center_hz + (k - self.nfft // 2) * self.bin_hz
        self._f0 = float(freq[0])

        # relayed-noise rise R_k and guard bins
        rise_lin = np.zeros(self.nfft)
        in_passband = np.zeros(self.nfft, dtype=bool)
        for i, band in enumerate(passbands):
            try:
                f_lo, f_hi, rise_db = band
            except (TypeError, ValueError):
                raise ValueError(f"passbands[{i}] must be (f_lo_hz, f_hi_hz, relay_rise_db)") from None
            f_lo, f_hi = _finite(f_lo, f"passbands[{i}].f_lo_hz"), _finite(f_hi, f"passbands[{i}].f_hi_hz")
            rise_db = _finite(rise_db, f"passbands[{i}].relay_rise_db")
            if f_hi <= f_lo:
                raise ValueError(f"passbands[{i}] needs f_lo_hz < f_hi_hz (got {f_lo!r}, {f_hi!r})")
            if rise_db < 0.0:
                raise ValueError(f"passbands[{i}].relay_rise_db must be >= 0 (got {rise_db!r})")
            inside = (freq >= f_lo) & (freq <= f_hi)
            rise_lin[inside] += 10.0 ** (rise_db / 10.0) - 1.0
            in_passband |= inside
        self._rise_db = 10.0 * np.log10(1.0 + rise_lin)

        # authorised carriers: inclusive bin ranges, widened by the Hann main lobe
        self._carriers: list[tuple[str, int, int]] = []
        is_carrier = np.zeros(self.nfft, dtype=bool)
        for i, carrier in enumerate(carriers):
            try:
                carrier_id, f_lo, f_hi = carrier
            except (TypeError, ValueError):
                raise ValueError(f"carriers[{i}] must be (carrier_id, f_lo_hz, f_hi_hz)") from None
            f_lo, f_hi = _finite(f_lo, f"carriers[{i}].f_lo_hz"), _finite(f_hi, f"carriers[{i}].f_hi_hz")
            if f_hi <= f_lo:
                raise ValueError(f"carriers[{i}] needs f_lo_hz < f_hi_hz (got {f_lo!r}, {f_hi!r})")
            k_lo = int(math.ceil((f_lo - self._f0) / self.bin_hz - 1e-9))
            k_hi = int(math.floor((f_hi - self._f0) / self.bin_hz + 1e-9))
            if k_hi < k_lo:  # narrower than a bin and between two centres: nearest bin
                k_lo = k_hi = int(round((0.5 * (f_lo + f_hi) - self._f0) / self.bin_hz))
            k_lo, k_hi = k_lo - CARRIER_PAD_BINS, k_hi + CARRIER_PAD_BINS
            if k_hi < 0 or k_lo > self.nfft - 1:
                continue
            k_lo, k_hi = max(k_lo, 0), min(k_hi, self.nfft - 1)
            self._carriers.append((str(carrier_id), k_lo, k_hi))
            is_carrier[k_lo : k_hi + 1] = True

        # floor estimator: guard-bin median, or a corrected low percentile of everything
        guard = ~in_passband & ~is_carrier
        n_guard = int(np.count_nonzero(guard))
        self._use_guard = n_guard >= MIN_GUARD_BINS
        if self._use_guard:
            self._floor_mask = guard
            self._min_floor_bins = min(MIN_FLOOR_BINS, n_guard // 2)
        else:
            free = ~is_carrier
            self._floor_mask = free if int(np.count_nonzero(free)) >= MIN_FLOOR_BINS else np.ones(self.nfft, bool)
            self._min_floor_bins = MIN_FLOOR_BINS
        self._floor_idx = np.flatnonzero(self._floor_mask)
        self._floor_recent: deque[float] = deque(maxlen=FLOOR_SMOOTH_FRAMES)
        self._floor_mean = math.nan  # F of the latest frame

        # threshold in force: threshold_db, or what the frames' own statistics require
        self._scatter_db, self._offset_db, self._least_db, self._mean_over_median_db = _fluctuation_table()
        # floor quantiles: the guard median (or the corrected 10th percentile), then lower
        self._floor_start = 0.5 if self._use_guard else FLOOR_PERCENTILE
        self._floor_quantiles = (self._floor_start, *(p for p in FLOOR_LOWER_QUANTILES if p < self._floor_start))
        self._quantiles = _quantile_table(self._floor_quantiles)
        self._floor_now = math.nan  # this frame's floor estimate before smoothing, dB
        self._scatter_recent: deque[float] = deque(maxlen=SCATTER_FRAMES)
        self._threshold_eff = self._threshold_db
        self._mean_over_median = 1.0  # linear; mean of a frame's bins over their median
        self._empty_ratio = 10.0 ** (EMPTY_FRACTION * self._threshold_db / 10.0)

        # per-bin history (rows are bins so the per-bin sort runs over contiguous memory)
        self._hist = np.full((self.nfft, self._capacity), np.inf, dtype=np.float32)
        self._count = np.zeros(self.nfft, dtype=np.intp)
        self._pos = np.zeros(self.nfft, dtype=np.intp)
        self._hist_median = np.zeros(self.nfft)
        self._rows = np.arange(self.nfft)
        # entries needed before the history replaces the prior / before a bin is tested
        self._need_use = np.where(is_carrier, 1, learn_entries)
        self._need_test = np.where(is_carrier, carrier_entries, 0)
        self._use_hist = np.zeros(self.nfft, dtype=bool)
        self._testable = ~is_carrier
        self._last_keep_ns: int | None = None

        self._tracks: list[_Track] = []
        self._recent: deque[npt.NDArray[np.float64]] = deque(maxlen=self._confirm_n)
        self._next_id = 1
        self._last_t_ns: int | None = None
        self._baseline = np.full(self.nfft, np.nan)
        self._in_track = np.zeros(self.nfft, dtype=bool)  # track extents and skirts, previous frame
        self._any_track = False
        self._age = np.zeros(self.nfft, dtype=np.int64)  # frames since each bin was last above threshold
        # above-threshold masks of the previous confirm_n frames (None: nothing was above)
        self._above_recent: deque[npt.NDArray[np.bool_] | None] = deque(maxlen=self._confirm_n)
        self._patience = self._release_frames + self._confirm_n
        self._split_gap = SPLIT_GAP_RUNS * (self._merge_bins + 1)

    # ------------------------------------------------------------------ public

    @property
    def baseline_db(self) -> npt.NDArray[np.float32]:
        """Baseline the latest frame was tested against, float32[nfft] dB (NaN before the first frame)."""
        return self._baseline.astype(np.float32)

    @property
    def effective_threshold_db(self) -> float:
        """Threshold the latest frame was tested with, dB above the baseline.

        cfg.threshold_db, or the level a noise bin exceeds with probability
        NOISE_EXCEEDANCE when that is higher (frames of fewer than five averages
        with the default 6 dB).
        """
        return self._threshold_eff

    @property
    def floor_db(self) -> float:
        """The start-up floor F of the latest frame, dB (NaN before the first frame).

        The median noise level of a bin without relayed noise: the guard-bin median,
        or the corrected low quantile of the window less the relayed-noise rise
        (averaged over the last FLOOR_SMOOTH_FRAMES estimates; held while a signal
        covers the floor bins). The baseline of a bin still on its prior is F + R_k.
        Windows of one receiver and span share the same F, so a window whose F lies
        well above the others' is filled by something it may be taking for noise.
        """
        return self._floor_mean  # one float, safe to read while an update runs elsewhere

    @property
    def floor_from_guard(self) -> bool:
        """Whether F is the median of guard bins (bins outside every passband and carrier)."""
        return self._use_guard

    @property
    def learning(self) -> bool:
        """Whether most bins are still judged against the start-up prior (F + R_k) rather
        than their own history (the first PRIOR_SPAN_FRACTION of baseline_tau_s)."""
        return bool(np.count_nonzero(self._use_hist) * 2 < self.nfft)

    def reset_time(self) -> None:
        """Accept the next frame whatever its time (the frame clock stepped).

        The baseline, the history and the tracks are kept; the history keep schedule
        restarts at the next frame. Used when a node's clock is corrected backwards or
        a new session of a node starts: without it every frame older than the newest
        one seen would be refused.
        """
        self._last_t_ns = None
        self._last_keep_ns = None

    def update(self, psd_db: npt.ArrayLike, t_ns: int) -> list[Detection]:
        """Process one PSD frame and return every confirmed, unreleased track.

        psd_db: float[nfft] in dB, fft-shifted (`welch_psd_db` units), all finite.
        t_ns: first-sample time of the frame, integer ns, strictly increasing (after
            `reset_time` any value is accepted once).
        Returns the detections ordered by f_lo_hz. A track on an authorised carrier is
        listed only while its anomaly is asserted.
        """
        if np.iscomplexobj(psd_db):
            raise ValueError("psd_db must be real dB values (got complex)")
        try:
            psd = np.array(psd_db, dtype=np.float64)  # a copy: the last confirm_n frames are kept
        except (TypeError, ValueError):
            raise ValueError(f"psd_db must be an array of {self.nfft} numbers") from None
        if psd.shape != (self.nfft,):
            raise ValueError(f"psd_db must have shape ({self.nfft},), got {psd.shape}")
        if not np.isfinite(psd).all():
            raise ValueError("psd_db must be finite in every bin")
        if isinstance(t_ns, bool) or not isinstance(t_ns, (int, np.integer)):
            raise ValueError(f"t_ns must be an integer number of nanoseconds (got {t_ns!r})")
        t_ns = int(t_ns)
        if self._last_t_ns is not None and t_ns <= self._last_t_ns:
            raise ValueError(f"t_ns must increase from frame to frame (got {t_ns} after {self._last_t_ns})")
        self._last_t_ns = t_ns
        self._recent.append(psd)

        self._calibrate(psd)
        floor = self._floor(psd)
        baseline = np.where(self._use_hist, self._hist_median, floor + self._rise_db)
        self._baseline = baseline
        above = psd > baseline + self._threshold_eff
        above &= self._testable
        self._age += 1
        self._age[above] = 0

        frozen = above  # bins in detection: above threshold now, or inside a track (below)
        any_above = bool(above.any())
        if self._tracks or any_above:
            self._update_tracks(above, t_ns)
            self._measure(psd, baseline, t_ns)
            in_track = self._in_track
            in_track[:] = False
            for tr in self._tracks:
                in_track[max(tr.lo - SKIRT_BINS, 0) : tr.hi + SKIRT_BINS + 1] = True
            self._above_recent.append(above.copy() if any_above else None)
            frozen |= in_track
            self._any_track = bool(self._tracks)
        else:
            self._above_recent.append(None)

        if self._last_keep_ns is None or t_ns - self._last_keep_ns >= self._keep_ns - self._keep_tol_ns:
            self._last_keep_ns = t_ns
            self._remember(psd, frozen)

        return self._detections()

    # ----------------------------------------------------------------- baseline

    def _calibrate(self, psd: npt.NDArray[np.float64]) -> None:
        """Set the threshold in force from the bin-to-bin scatter of the frames.

        The scatter of a frame is the median absolute dB difference of bins _LAG
        apart (pairs touching a track are left out; the median ignores the edges of
        whatever else is in the window), averaged over the last SCATTER_FRAMES
        frames. It tells the number of averages behind a frame, and with it the level
        over the median that a noise bin exceeds with probability NOISE_EXCEEDANCE and
        the mean-to-median ratio of a bin.
        """
        x = psd - self._rise_db
        step = np.abs(x[_LAG:] - x[:-_LAG])
        if self._any_track:
            step = step[~(self._in_track[_LAG:] | self._in_track[:-_LAG])]
        if step.size >= MIN_SCATTER_PAIRS:
            self._scatter_recent.append(float(np.median(step)))
        if self._scatter_recent:
            scatter = math.fsum(self._scatter_recent) / len(self._scatter_recent)
            least = float(np.interp(scatter, self._scatter_db, self._least_db))
            self._threshold_eff = max(self._threshold_db, least)
            ratio_db = float(np.interp(scatter, self._scatter_db, self._mean_over_median_db))
            self._mean_over_median = 10.0 ** (ratio_db / 10.0)

    def _floor(self, psd: npt.NDArray[np.float64]) -> float:
        """Start-up floor F in dB: this frame's estimate averaged with the previous four.

        Like the history, the estimate leaves out bins in detection: those inside a
        track and those more than the threshold above the baseline of the previous
        frame. When too few remain (an emitter over the whole window) F is held.
        """
        idx: npt.NDArray[np.intp] | None = self._floor_idx
        if self._floor_recent:
            busy = psd > self._baseline + self._threshold_eff
            if self._any_track:
                busy |= self._in_track
            if busy.any():
                idx = np.flatnonzero(self._floor_mask & ~busy)
                if idx.size < self._min_floor_bins:
                    idx = None
        if idx is not None:
            if self._use_guard:
                x = psd[idx]
            else:
                x = psd[idx] - self._rise_db[idx]
            now, sigma, stepped = self._floor_estimate(x)
            # A signal lifted the usual quantile: leave out what lies more than the
            # threshold above the estimate and estimate again (a few times at most), so
            # that a signal over most of the window is found in its first frames.
            for _ in range(FLOOR_REFINE_STEPS):
                if not stepped:
                    break
                keep = x <= now + self._threshold_eff
                if keep.all() or int(np.count_nonzero(keep)) < self._min_floor_bins:
                    break
                x = x[keep]
                refined, refined_sigma, stepped = self._floor_estimate(x)
                if refined >= now:
                    break
                now, sigma = refined, refined_sigma
            self._floor_now = now
            if self._floor_recent:
                smoothed = math.fsum(self._floor_recent) / len(self._floor_recent)
                if now < smoothed - FLOOR_SIGNIFICANCE * math.sqrt(2.0) * sigma:
                    # the earlier estimates were lifted by a signal now left out
                    self._floor_recent.clear()
            self._floor_recent.append(now)
        self._floor_mean = math.fsum(self._floor_recent) / len(self._floor_recent)
        return self._floor_mean

    def _floor_estimate(self, x: npt.NDArray[np.float64]) -> tuple[float, float, bool]:
        """Median noise level of the floor bins `x` (dB, relayed-noise rise removed),
        its standard error (dB), and whether a lower quantile was used.

        The guard median, or without guard bins the 10th percentile raised by its
        distance to the median (from the frames' fluctuation statistics). A signal
        already on when the tracker starts can cover most of those bins and lift that
        quantile with it: the estimate then steps down to a lower quantile (corrected
        the same way) whenever that one lies lower by more than FLOOR_SIGNIFICANCE
        standard errors, which noise alone almost never does. A lower quantile is tried
        only when FLOOR_LEAST_BINS bins lie below it, so a signal is found against the
        floor as long as it leaves the bins below the lowest quantile tried free.
        """
        count = int(x.size)
        if count < 2:
            return (float(x[0]) if count else 0.0), math.inf, False
        if self._use_guard and self._scatter_recent:
            scatter = math.fsum(self._scatter_recent) / len(self._scatter_recent)
        else:  # the frame's own scatter (guard bins can be too few to give one alone)
            diffs = np.abs(x[_LAG:] - x[:-_LAG]) if count > _LAG else np.abs(np.diff(x))
            scatter = float(np.median(diffs))
        n_eff = 0.5 * count  # neighbouring Hann-windowed bins are correlated
        tried = [
            (p, int(p * (count - 1)))
            for i, p in enumerate(self._floor_quantiles)
            if i == 0 or p * count >= FLOOR_LEAST_BINS
        ]
        part = np.partition(x, sorted({k for _, k in tried}))
        best = sigma_best = math.nan
        stepped = False
        for i, (p, k) in enumerate(tried):
            offset_db, density = self._quantiles[p]
            if p == 0.5:
                level, offset = float(np.median(x)), 0.0
            else:
                level, offset = float(part[k]), float(np.interp(scatter, self._scatter_db, offset_db))
            estimate = level + offset
            sigma = math.sqrt(p * (1.0 - p) / n_eff) / max(float(np.interp(scatter, self._scatter_db, density)), _TINY)
            if i == 0 or estimate < best - FLOOR_SIGNIFICANCE * math.hypot(sigma, sigma_best):
                best, sigma_best, stepped = estimate, sigma, i > 0
        return best, sigma_best, stepped

    def _remember(self, psd: npt.NDArray[np.float64], frozen: npt.NDArray[np.bool_]) -> None:
        """Append this frame to the history of every bin that is not in detection."""
        upd = np.flatnonzero(~frozen)
        if upd.size == 0:
            return
        pos = self._pos[upd]
        self._hist[upd, pos] = psd[upd]
        self._pos[upd] = (pos + 1) % self._capacity
        count = self._count
        count[upd] = np.minimum(count[upd] + 1, self._capacity)
        ordered = np.sort(self._hist, axis=1)  # unused slots hold +inf and sort last
        low = ordered[self._rows, np.maximum(count - 1, 0) // 2].astype(np.float64)
        high = ordered[self._rows, count // 2].astype(np.float64)
        self._hist_median = np.where(count > 0, 0.5 * (low + high), 0.0)
        self._use_hist = count >= self._need_use
        self._testable = count >= self._need_test

    # ------------------------------------------------------------------- tracks

    def _update_tracks(self, above: npt.NDArray[np.bool_], t_ns: int) -> None:
        """Associate this frame's runs with the tracks; create, merge, split, confirm and release."""
        self._split_tracks(t_ns)
        first, last = _runs(above, self._merge_bins)
        tracks = self._tracks
        nt, nr = len(tracks), int(first.size)
        reach = self._merge_bins + 1
        parent = list(range(nt))

        def find(j: int) -> int:
            while parent[j] != j:
                parent[j] = parent[parent[j]]
                j = parent[j]
            return j

        runs_of: list[list[tuple[int, int]]] = [[] for _ in range(nt)]
        shared: list[tuple[list[int], int, int]] = []
        created: list[_Track] = []
        # a run belongs to every track whose extent it overlaps or lies within merge_bins of
        links: list[list[int]] = [[] for _ in range(nr)]
        if nr and nt:
            t_lo = np.fromiter((tr.lo for tr in tracks), dtype=np.intp, count=nt)
            t_hi = np.fromiter((tr.hi for tr in tracks), dtype=np.intp, count=nt)
            near = (t_lo[None, :] - reach <= last[:, None]) & (t_hi[None, :] + reach >= first[:, None])
            for r, j in zip(*(index.tolist() for index in np.nonzero(near))):
                links[r].append(j)
        for (s, e), linked in zip(zip(first.tolist(), last.tolist()), links):
            if not linked:
                created.append(
                    _Track(
                        lo=s,
                        hi=e,
                        first_seen_ns=t_ns,
                        last_seen_ns=t_ns,
                        hits=deque([True], maxlen=self._confirm_n),
                        extents=deque([(s, e)], maxlen=self._confirm_n),
                    )
                )
                continue
            if len(linked) == 1:
                runs_of[linked[0]].append((s, e))
                continue
            confirmed = [j for j in linked if tracks[j].confirmed]
            root = confirmed[0] if confirmed else linked[0]
            for j in linked:
                if not tracks[j].confirmed:
                    # an unconfirmed fragment joins the nearest confirmed track, so a fragment
                    # beyond a second confirmed track does not stretch the first over it
                    near = root
                    if confirmed:
                        frag = tracks[j]
                        near = min(confirmed, key=lambda c: max(tracks[c].lo - frag.hi, frag.lo - tracks[c].hi))
                    parent[find(j)] = find(near)
            if len(confirmed) >= 2:
                shared.append((confirmed, s, e))
            else:
                runs_of[root].append((s, e))

        # confirmed tracks sharing a run: merge only when it has lasted confirm_m frames
        sharing = {j for group, _, _ in shared for j in group}
        for j, tr in enumerate(tracks):
            tr.fuse = tr.fuse + 1 if j in sharing else 0
        for group, s, e in shared:
            if all(tracks[j].fuse >= self._confirm_m for j in group):
                for j in group[1:]:
                    parent[find(j)] = find(group[0])
                runs_of[group[0]].append((s, e))
            else:
                # not merged (yet): each track takes the part of the run on its own side of
                # the midpoints between neighbouring tracks, so extents never overlap
                side = sorted(group, key=lambda j: tracks[j].lo)
                for i, j in enumerate(side):
                    tr = tracks[j]
                    lo, hi = max(s, tr.lo - reach), min(e, tr.hi + reach)
                    if i > 0:
                        lo = max(lo, (tracks[side[i - 1]].hi + tr.lo) // 2 + 1)
                    if i + 1 < len(side):
                        hi = min(hi, (tr.hi + tracks[side[i + 1]].lo) // 2)
                    if lo <= hi:
                        runs_of[j].append((lo, hi))

        # resolve merges: the confirmed track with the lowest id (else the oldest) survives
        members: dict[int, list[int]] = {}
        for j in range(nt):
            members.setdefault(find(j), []).append(j)
        kept: list[_Track] = []
        for group in members.values():
            if len(group) == 1:
                tr = tracks[group[0]]
                frame_runs = runs_of[group[0]]
            else:
                order = sorted(
                    group,
                    key=lambda j: (
                        not tracks[j].confirmed,
                        tracks[j].track_id,
                        tracks[j].first_seen_ns,
                        tracks[j].lo,
                    ),
                )
                tr = tracks[order[0]]
                frame_runs = [run for j in group for run in runs_of[j]]
                for j in order[1:]:
                    other = tracks[j]
                    if other.confirmed:  # remembered, so the merge can be undone
                        tr.absorbed.append((other.track_id, other.home, other.own_first_seen_ns))
                        tr.absorbed.extend(other.absorbed)
                    tr.first_seen_ns = min(tr.first_seen_ns, other.first_seen_ns)
                    tr.last_seen_ns = max(tr.last_seen_ns, other.last_seen_ns)
                    if not tr.confirmed:
                        mine, theirs = list(tr.hits), list(other.hits)
                        width = max(len(mine), len(theirs))
                        mine = [False] * (width - len(mine)) + mine
                        theirs = [False] * (width - len(theirs)) + theirs
                        tr.hits = deque((a or b for a, b in zip(mine, theirs)), maxlen=self._confirm_n)
                    tr.lo, tr.hi = min(tr.lo, other.lo), max(tr.hi, other.hi)
            if frame_runs:
                extent: tuple[int, int] | None = (
                    min(run[0] for run in frame_runs),
                    max(run[1] for run in frame_runs),
                )
                tr.misses = 0
                tr.last_seen_ns = t_ns
            else:
                extent = None
                tr.misses += 1
            tr.hits.append(extent is not None)
            tr.extents.append(extent)
            seen = [ext for ext in tr.extents if ext is not None]
            if seen:
                tr.lo, tr.hi = min(ext[0] for ext in seen), max(ext[1] for ext in seen)
            if tr.confirmed:
                if tr.misses >= self._release_frames:
                    continue
            elif not any(tr.hits):
                continue
            kept.append(tr)
        kept.extend(created)
        for tr in kept:
            if not tr.confirmed and sum(tr.hits) >= self._confirm_m:
                tr.confirmed = True
                tr.track_id = self._next_id
                tr.own_first_seen_ns = tr.first_seen_ns
                self._next_id += 1
            if tr.absorbed:
                tr.absorbed = [part for part in tr.absorbed if part[1][0] <= tr.hi and part[1][1] >= tr.lo]
                if tr.absorbed and (tr.home[0] > tr.hi or tr.home[1] < tr.lo):
                    # the signal this track is named after is gone; what remains is one
                    # it had absorbed, which takes its own id and first-seen time back
                    tr.absorbed.sort()
                    tr.track_id, tr.home, tr.first_seen_ns = tr.absorbed.pop(0)
                    tr.own_first_seen_ns = tr.first_seen_ns
            if not tr.absorbed:
                tr.home = (tr.lo, tr.hi)
        kept.sort(key=lambda tr: tr.lo)
        self._tracks = kept

    def _split_tracks(self, t_ns: int) -> None:
        """Split confirmed tracks at stretches that have not been above threshold for long.

        A stretch of more than SPLIT_GAP_RUNS * (merge_bins + 1) bins inside a track's
        extent, none of them above threshold in the last release_frames + confirm_n
        frames, is not part of a signal: the track is cut there. (The margin over
        merge_bins keeps a wide signal near the threshold, whose bins come and go, in
        one piece.) A narrower stretch, down to merge_bins + 1 bins, is cut as well
        when its mean power over the last confirm_n frames is within EMPTY_FRACTION *
        threshold_db of its baseline: that is a gap between two signals, which a new
        tracker would report apart, not a weak part of one.
        A piece on the former extent of the track itself, or of a track
        that was merged into it, carries on under that id and first-seen time, with
        its own count of misses. Any other piece starts again as an unconfirmed track
        with its own record of the last confirm_n frames, so a real signal is back
        under a new id at once and a stray noise bin is never reported.
        """
        out: list[_Track] = []
        changed = False
        sum_recent: npt.NDArray[np.float64] | None = None
        sum_base: npt.NDArray[np.float64] | None = None
        for tr in self._tracks:
            if not tr.confirmed or tr.hi - tr.lo < self._merge_bins + 2:
                out.append(tr)
                continue
            live = self._age[tr.lo : tr.hi + 1] < self._patience
            if live.all() or not live.any():
                out.append(tr)
                continue
            # quiet stretches between live bins: (first, last) relative to tr.lo
            gap_lo, gap_hi = _runs(~live, 0)
            inner = (gap_lo > 0) & (gap_hi < live.size - 1)
            gap_lo, gap_hi = gap_lo[inner], gap_hi[inner]
            width = gap_hi - gap_lo + 1
            cut = width > self._split_gap
            narrow = ~cut & (width > self._merge_bins)
            if narrow.any():
                # a narrow stretch is a gap only when its power is back at the baseline
                if sum_recent is None or sum_base is None:
                    recent = np.exp(_DB_TO_NEPER * np.stack(self._recent)).mean(axis=0)
                    sum_recent = np.concatenate(([0.0], np.cumsum(recent)))
                    sum_base = np.concatenate(([0.0], np.cumsum(np.exp(_DB_TO_NEPER * self._baseline))))
                a, b = tr.lo + gap_lo[narrow], tr.lo + gap_hi[narrow] + 1
                cut[narrow] = sum_recent[b] - sum_recent[a] <= self._empty_ratio * (sum_base[b] - sum_base[a])
            if not cut.any():
                out.append(tr)
                continue
            changed = True
            alive = np.flatnonzero(live)
            starts = np.concatenate(([alive[0]], gap_hi[cut] + 1))
            ends = np.concatenate((gap_lo[cut] - 1, [alive[-1]]))
            pieces = [(tr.lo + a, tr.lo + b) for a, b in zip(starts.tolist(), ends.tolist())]
            # every former track goes to the piece it overlaps most
            owners: list[list[tuple[int, tuple[int, int], int]]] = [[] for _ in pieces]
            for part in [(tr.track_id, tr.home, tr.own_first_seen_ns), *tr.absorbed]:
                overlap = [min(hi, part[1][1]) - max(lo, part[1][0]) + 1 for lo, hi in pieces]
                best = max(range(len(pieces)), key=overlap.__getitem__)
                if overlap[best] > 0:
                    owners[best].append(part)
            for (lo, hi), parts in zip(pieces, owners):
                parts.sort()
                seen = [mask is not None and bool(mask[lo : hi + 1].any()) for mask in self._above_recent]
                piece = _Track(
                    lo=lo,
                    hi=hi,
                    first_seen_ns=t_ns,
                    last_seen_ns=tr.last_seen_ns,
                    hits=deque(seen, maxlen=self._confirm_n),
                    extents=deque([(lo, hi)], maxlen=self._confirm_n),
                    # frames since the piece was last above threshold, before this one
                    misses=max(int(self._age[lo : hi + 1].min()) - 1, 0),
                    power_db=tr.power_db,
                    snr_db=tr.snr_db,
                    cn0_dbhz=tr.cn0_dbhz,
                )
                if parts:
                    piece.confirmed = True
                    piece.track_id, piece.home, piece.first_seen_ns = parts[0]
                    piece.own_first_seen_ns = piece.first_seen_ns
                    piece.absorbed = parts[1:]
                out.append(piece)
        if changed:
            self._tracks = out

    def _measure(self, psd: npt.NDArray[np.float64], baseline: npt.NDArray[np.float64], t_ns: int) -> None:
        """Power, SNR and C/N0 of confirmed tracks seen in this frame, and the carrier rule.

        Sums over bin ranges are differences of running sums, so the cost per track does
        not depend on its width.
        """
        if not any(tr.confirmed for tr in self._tracks):
            return
        zero = np.zeros(1)
        p = np.exp(_DB_TO_NEPER * psd)
        b = np.exp(_DB_TO_NEPER * baseline)
        sum_p = np.concatenate((zero, np.cumsum(p)))
        sum_b = np.concatenate((zero, np.cumsum(b)))
        sum_excess = np.concatenate((zero, np.cumsum(np.maximum(p - b, 0.0))))
        sum_recent: npt.NDArray[np.float64] | None = None
        for tr in self._tracks:
            if not tr.confirmed:
                continue
            lo, hi = tr.lo, tr.hi + 1
            if tr.last_seen_ns == t_ns:
                mean_p = max(float(sum_p[hi] - sum_p[lo]) / (hi - lo), _TINY)
                mean_b = max(float(sum_b[hi] - sum_b[lo]) / (hi - lo), _TINY)
                excess = float(sum_excess[min(hi + SKIRT_BINS, self.nfft)] - sum_excess[max(lo - SKIRT_BINS, 0)])
                tr.power_db = 10.0 * math.log10(mean_p)
                tr.snr_db = tr.power_db - 10.0 * math.log10(mean_b)
                tr.cn0_dbhz = 10.0 * math.log10(max(excess / mean_b * self.bin_hz, _TINY))

            # authorised carrier: overlap of at least half the track's own width
            best, best_overlap, c_lo, c_hi = -1, 0, lo, hi
            for i, (_, k_lo, k_hi) in enumerate(self._carriers):
                overlap = min(hi - 1, k_hi) - max(lo, k_lo) + 1
                if overlap > best_overlap and 2 * overlap >= hi - lo:
                    best, best_overlap, c_lo, c_hi = i, overlap, max(lo, k_lo), min(hi - 1, k_hi) + 1
            if best != tr.carrier:
                tr.carrier, tr.anomaly, tr.quiet = best, False, 0
            if best < 0:
                continue
            # Power over the shared bins, averaged over the last confirm_n frames: the
            # bins of one frame's run are those that happened to exceed the threshold,
            # so a single frame would always read more than threshold_db. The baseline
            # is a median; it is compared as the mean it stands for (frame statistics
            # from _calibrate), so an unchanged carrier reads 0 dB, not +0.18 dB at
            # navg 8 (+1.6 dB at navg 1).
            if sum_recent is None:
                recent = np.exp(_DB_TO_NEPER * np.stack(self._recent)).mean(axis=0)
                sum_recent = np.concatenate((zero, np.cumsum(recent)))
            rise_db = 10.0 * math.log10(
                max(float(sum_recent[c_hi] - sum_recent[c_lo]), _TINY)
                / max(float(sum_b[c_hi] - sum_b[c_lo]) * self._mean_over_median, _TINY)
            )
            if rise_db > self._power_anomaly_db + _DB_TOL:
                tr.anomaly, tr.quiet = True, 0
            elif tr.anomaly:
                tr.quiet += 1
                if tr.quiet >= self._release_frames:
                    tr.anomaly = False

    def _detections(self) -> list[Detection]:
        out: list[Detection] = []
        for tr in self._tracks:
            if not tr.confirmed or (tr.carrier >= 0 and not tr.anomaly):
                continue
            out.append(
                Detection(
                    track_id=tr.track_id,
                    f_lo_hz=self._f0 + (tr.lo - 0.5) * self.bin_hz,
                    f_hi_hz=self._f0 + (tr.hi + 0.5) * self.bin_hz,
                    power_db=tr.power_db,
                    snr_db=tr.snr_db,
                    cn0_dbhz=tr.cn0_dbhz,
                    first_seen_ns=tr.first_seen_ns,
                    last_seen_ns=tr.last_seen_ns,
                    authorized_carrier_id=self._carriers[tr.carrier][0] if tr.carrier >= 0 else None,
                    anomaly=tr.carrier >= 0 and tr.anomaly,
                )
            )
        return out


# ------------------------------------------------------------------- classification

CLASS_LABELS: tuple[str, ...] = ("cw", "swept", "noise", "modulated", "pulsed", "unknown")
"""Every label `classify` can return."""

MIN_CLASSIFY_SAMPLES: int = 1024
"""Shortest capture `classify` accepts."""

MAX_CLASSIFY_SAMPLES: int = 262144
"""Samples of a capture that `classify` looks at (the first ones)."""

_PSD_MAX_BINS = 1024  # Welch resolution used to find the occupied band
_PSD_MIN_AVG = 16  # segment lengths the shortest capture must hold
_OCCUPIED_OVER_FLOOR = 2.0  # smoothed PSD over the noise floor that counts as occupied
_MIN_SIGNAL_OVER_NOISE = 0.1  # least signal over noise, in the band kept, worth classifying
_LINE_MARGIN = 14.0  # a spectral line needs peak / mean >= ln(bins) + this (false alarm e^-14)
_LINE_SHARE = 0.8  # share of the signal power a line must hold to be a bare carrier
_COHERENCE_TIMES = 16.0  # envelope smoothing length in units of 1 / occupied bandwidth
_PULSE_CONTRAST_DB = 6.0  # on-level over off-level of the smoothed envelope
_PULSE_MIN_SHARE = 0.04  # least fraction of time spent on, and off
_PULSE_MIN_SMOOTH = 32  # shortest envelope smoothing, samples
_SWEEP_SCALES = (16, 32, 64, 128, 256, 512, 1024)  # short-time FFT lengths tried
_SWEEP_MAX_SAMPLES = 65536
_SWEEP_MIN_BINS = 8.0  # the average spectrum must cover this many short-time bins
_SWEEP_CONCENTRATION = 1.8  # time-frequency concentration that marks a swept tone
_SWEEP_LINEARITY = 0.5  # share of ridge steps that must follow one sweep rate
_SYMBOL_BAND = (0.3, 1.25)  # symbol-rate search range in units of the occupied bandwidth
_SYMBOL_MARGIN = 14.0  # peak / local mean >= ln(bins) + this
_CFAR_WINDOW = 64  # bins on each side that give the local mean of the envelope spectrum
_CFAR_GUARD = 2  # bins skipped next to the bin under test (Hann main lobe)
_KURTOSIS_SLACK = 0.2  # envelope kurtosis within this of 2 (below; 1.5 times it above) is Gaussian


@dataclass
class Classification:
    """Waveform class of a capture.

    label: one of CLASS_LABELS.
    confidence: 0..1; `unknown` always carries 0.
    features: finite floats (JSON-safe), see `classify`.
    """

    label: str
    confidence: float
    features: dict[str, float] = field(default_factory=dict)


def _hann(n: int) -> npt.NDArray[np.float64]:
    """Periodic Hann window of length n."""
    return 0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(n) / n)


def _db(ratio: float, floor_db: float = -60.0, ceil_db: float = 200.0) -> float:
    """10 log10 of a power ratio, clipped so the result is always finite."""
    if not ratio > 0.0:
        return floor_db
    return float(min(max(10.0 * math.log10(ratio), floor_db), ceil_db))


def _clip01(x: float) -> float:
    return float(min(max(x, 0.0), 1.0))


def _overlapped_psd(x: npt.NDArray[np.complex128], nfft: int) -> tuple[npt.NDArray[np.float64], float]:
    """Welch PSD with 75 % overlapping Hann segments, in `welch_psd_db` normalisation.

    Unit-variance white noise reads 1 (linear) in every bin; fft-shifted. The squared
    Hann window sums to a constant at this overlap, so every instant of the capture
    has the same weight: with non-overlapping segments a sweep whose period is close
    to the segment length would show the window shape instead of its flat spectrum.
    Returns (psd[nfft], effective number of independent averages).
    """
    hop = nfft // 4
    window = _hann(nfft)
    spec = scipy.fft.fft(sliding_window_view(x, nfft)[::hop] * window, axis=1)
    psd = (spec.real**2 + spec.imag**2).mean(axis=0) / float((window * window).sum())
    # segments 1, 2 and 3 hops apart correlate by 0.659, 0.167 and 0.0075 (Hann)
    segments = spec.shape[0]
    spread = 1.0 + 2.0 * sum(
        c * c * (segments - j) / segments for j, c in enumerate((0.659, 0.167, 0.0075), start=1) if j < segments
    )
    return scipy.fft.fftshift(psd), segments / spread


def _occupied_band(psd: npt.NDArray[np.float64], noise: float) -> tuple[int, int, int, int] | None:
    """Dominant occupied band of a linear, fft-shifted PSD.

    Returns (k_lo, k_hi, r_lo, r_hi), inclusive bins: the band between the half-level
    points of the strongest region, and that whole region (smoothed PSD more than
    _OCCUPIED_OVER_FLOOR times the floor). None when nothing rises that far.
    """
    nb = psd.size
    width = max(3, nb // 64) | 1
    kernel = np.ones(width)
    smooth = np.convolve(psd, kernel, mode="same") / np.convolve(np.ones(nb), kernel, mode="same")
    first, last = _runs(smooth > _OCCUPIED_OVER_FLOOR * noise, width)
    if first.size == 0:
        return None
    excess = np.concatenate(([0.0], np.cumsum(psd - noise)))
    best = int(np.argmax(excess[last + 1] - excess[first]))
    r_lo, r_hi = int(first[best]), int(last[best])
    region = smooth[r_lo : r_hi + 1] - noise
    half = 0.5 * float(np.median(region[region > 0.0]))
    peak = r_lo + int(np.argmax(region))
    below = (smooth - noise) < half
    left = np.flatnonzero(below[:peak])
    right = np.flatnonzero(below[peak + 1 :])
    k_lo = int(left[-1]) + 1 if left.size else 0
    k_hi = peak + int(right[0]) if right.size else nb - 1
    return k_lo, k_hi, r_lo, r_hi


def _envelope_levels(power: npt.NDArray[np.float64], smooth: int) -> tuple[float, float, float, float, float]:
    """On/off statistics of an envelope.

    power: |x|^2 per sample. smooth: averaging length in samples.
    Returns (contrast_db, share_low, share_mid, share_high, duty). contrast_db is the
    distance from the 2nd to the 98th percentile of the smoothed power in dB; the
    shares are the fractions of time spent in the lower, middle and upper third of
    that range. A gated signal sits in the two outer thirds; any single-mode envelope
    has more in the middle than at the bottom. duty is the fraction of time above the
    mean of the off level and the on level (the medians of the outer thirds, linear
    power): a boxcar-smoothed gate crosses that level exactly at its edges.
    """
    csum = np.concatenate(([0.0], np.cumsum(power)))
    starts = np.arange(0, power.size - smooth + 1, max(1, smooth // 2))
    mean = np.maximum((csum[starts + smooth] - csum[starts]) / smooth, _TINY)
    level = 10.0 * np.log10(mean)
    lo, hi = np.percentile(level, [2.0, 98.0])
    contrast = float(hi - lo)
    if contrast <= 0.0:
        return 0.0, 0.0, 1.0, 0.0, 1.0
    is_low = level < lo + contrast / 3.0
    is_high = level >= lo + 2.0 * contrast / 3.0
    low, high = float(np.mean(is_low)), float(np.mean(is_high))
    duty = 1.0
    if is_low.any() and is_high.any():
        duty = float(np.mean(mean > 0.5 * (float(np.median(mean[is_low])) + float(np.median(mean[is_high])))))
    return contrast, low, 1.0 - low - high, high, duty


def _symbol_line(power: npt.NDArray[np.float64], fs: float, f_lo: float, f_hi: float) -> tuple[float, float]:
    """Strongest spectral line of the squared envelope between f_lo and f_hi (Hz).

    Each bin of the envelope spectrum is compared with the mean of the _CFAR_WINDOW
    bins on its left and of those on its right, whichever is larger, so the edge of
    the continuous part of the spectrum is not taken for a line.
    Returns (line_db, f_line_hz): the largest such ratio over the level expected of
    the largest of that many noise bins plus _SYMBOL_MARGIN (so > 0 means a line), and
    the frequency of the peak. (-60, 0) when the range holds too few bins.
    """
    n = power.size
    reach = _CFAR_GUARD + _CFAR_WINDOW
    k1 = max(int(math.ceil(f_lo * n / fs)), reach + 1)
    k2 = min(int(math.floor(f_hi * n / fs)), n // 2 - reach - 2)
    if k2 - k1 < 4 * _CFAR_WINDOW:
        return -60.0, 0.0
    spec = scipy.fft.rfft((power - power.mean()) * _hann(n))
    csum = np.concatenate(([0.0], np.cumsum(spec.real**2 + spec.imag**2)))
    k = np.arange(k1, k2 + 1)
    left = csum[k - _CFAR_GUARD] - csum[k - reach]
    right = csum[k + reach + 1] - csum[k + _CFAR_GUARD + 1]
    ratio = (csum[k + 1] - csum[k]) * _CFAR_WINDOW / np.maximum(np.maximum(left, right), _TINY)
    peak = int(np.argmax(ratio))
    need = math.log(k.size) + _SYMBOL_MARGIN
    return _db(float(ratio[peak]) / need), float(k[peak]) * fs / n


def _sweep_features(x: npt.NDArray[np.complex128], fs: float) -> tuple[float, float, float]:
    """Time-frequency concentration and ridge linearity of a band-limited capture.

    Returns (concentration, linearity, sweep_rate_hz_per_s). concentration compares the
    number of short-time FFT bins the average spectrum covers with the number a single
    slice covers: about 1 for a stationary Gaussian signal, several for a tone that
    sweeps the same band. Only the more energetic half of the slices is looked at, so
    a sweep that leaves the capture band for part of its period still counts. It is
    the largest value over the short-time lengths in _SWEEP_SCALES at which the
    average spectrum covers at least _SWEEP_MIN_BINS bins (0 when there is none).
    linearity is the share of steps of the spectral peak between consecutive slices
    that equal the median step within one bin; sweep_rate is that median step, signed.
    """
    x = x[:_SWEEP_MAX_SAMPLES]
    best: tuple[float, float, float] = (0.0, 0.0, 0.0)
    for length in _SWEEP_SCALES:
        if length > x.size // 32:
            break
        hop = length // 2
        spec = scipy.fft.fft(sliding_window_view(x, length)[::hop] * _hann(length), axis=1)
        s = spec.real**2 + spec.imag**2
        mean = s.mean(axis=0)
        covered = float(mean.sum() ** 2 / max(float((mean * mean).sum()), _TINY))
        if covered < _SWEEP_MIN_BINS:
            continue
        energy = s.sum(axis=1)
        on = energy >= np.median(energy)
        slice_bins = energy[on] ** 2 / np.maximum((s[on] * s[on]).sum(axis=1), _TINY)
        concentration = 0.5 * covered / float(np.median(slice_bins))
        if concentration <= best[0]:
            continue
        # spectral peak of each slice, refined by a parabola through the log power around it
        peak = np.argmax(s, axis=1)
        rows = np.arange(peak.size)
        la, lb, lc = (np.log(np.maximum(s[rows, (peak + d) % length], _TINY)) for d in (-1, 0, 1))
        curve = la - 2.0 * lb + lc
        ridge = peak + np.where(curve < 0.0, 0.5 * (la - lc) / np.minimum(curve, -_TINY), 0.0)
        step = ((np.diff(ridge) + length / 2) % length - length / 2)[on[1:] & on[:-1]]  # bins per hop
        if step.size < 8:
            continue
        typical = float(np.median(np.abs(step)))
        linearity = float(np.mean(np.abs(np.abs(step) - typical) <= 1.0)) if typical >= 0.25 else 0.0
        sign = 1.0 if float(np.median(step)) >= 0.0 else -1.0
        best = (concentration, linearity, sign * typical * (fs / length) * (fs / hop))
    return best


def classify(iq: npt.ArrayLike, fs: float) -> Classification:
    """Classify the dominant signal of a short IQ capture (MATH.md section 7).

    iq: complex baseband samples, one dimension, at least MIN_CLASSIFY_SAMPLES of them
        (only the first MAX_CLASSIFY_SAMPLES are used); any amplitude scale.
    fs: sample rate, Hz.

    Labels, in the order they are decided: `cw` (one spectral line holds the signal
    power), `swept` (instantaneously narrow and moving steadily in frequency),
    `pulsed` (the envelope switches between an on and an off level), `modulated`
    (symbol-rate line, or an envelope that is not Gaussian), `noise` (band-limited
    Gaussian noise), `unknown` (nothing rises 3 dB above a flat noise floor and there
    is no spectral line, or no class fits). confidence is 0..1 and is 0 for `unknown`.

    The noise floor is read from the capture itself (lowest tenth of its spectrum), so
    a signal must leave part of the capture band free: noise filling the whole band is
    `unknown`. A signal narrower than about 2 % of the band is found only through a
    spectral line. Only the strongest signal in the capture is classified.

    features (all finite floats):
      snr_db               signal over noise inside the band the classifier keeps (the
                           occupied band and its skirts), dB; for `cw`, the tone over the
                           noise in the five full-resolution bins of its main lobe; -60 if none
      occupied_bw_hz       width between the half-level points of the spectrum, Hz
      center_offset_hz     centre of that band relative to the capture centre, Hz
      spectral_flatness    geometric over arithmetic mean of the PSD across the band, 0..1
      line_snr_db          strongest full-resolution spectral line over the noise floor, dB
      line_share           share of the signal power in that line, 0..1
      envelope_kurtosis    E|s|^4 / (E|s|^2)^2 of the signal with the noise removed
                           (1 constant envelope, 2 Gaussian)
      envelope_contrast_db on-level over off-level of the smoothed envelope, dB
      duty_cycle           fraction of time the signal is on (1 when not pulsed)
      tf_concentration     time-frequency concentration (see _sweep_features)
      if_linearity         linearity of the instantaneous frequency, 0..1
      sweep_rate_hz_per_s  signed sweep rate, Hz/s (0 unless swept)
      symbol_line_db       symbol-rate line of |x|^2 over its detection threshold, dB
      symbol_rate_hz       symbol rate from that line, Hz (0 when below threshold)

    Raises ValueError naming `iq` or `fs` for invalid input.
    """
    x = np.asarray(iq)
    if x.ndim != 1:
        raise ValueError(f"iq must be one-dimensional (got shape {x.shape})")
    if not np.issubdtype(x.dtype, np.number) or x.dtype == np.bool_:
        raise ValueError(f"iq must be numeric (got dtype {x.dtype})")
    if x.size < MIN_CLASSIFY_SAMPLES:
        raise ValueError(f"iq needs at least {MIN_CLASSIFY_SAMPLES} samples (got {x.size})")
    fs = _finite(fs, "fs")
    if fs <= 0.0:
        raise ValueError(f"fs must be > 0 (got {fs!r})")
    x = x[:MAX_CLASSIFY_SAMPLES].astype(np.complex128)
    if not np.isfinite(x).all():
        raise ValueError("iq must be finite")

    nfft = int(min(_PSD_MAX_BINS, 2 ** int(math.floor(math.log2(x.size / _PSD_MIN_AVG)))))
    x = x[: nfft * (x.size // nfft)]
    n = x.size
    power = x.real**2 + x.imag**2
    total = float(power.mean())
    feats: dict[str, float] = {
        "snr_db": -60.0,
        "occupied_bw_hz": 0.0,
        "center_offset_hz": 0.0,
        "spectral_flatness": 1.0,
        "line_snr_db": -60.0,
        "line_share": 0.0,
        "envelope_kurtosis": 2.0,
        "envelope_contrast_db": 0.0,
        "duty_cycle": 1.0,
        "tf_concentration": 0.0,
        "if_linearity": 0.0,
        "sweep_rate_hz_per_s": 0.0,
        "symbol_line_db": -60.0,
        "symbol_rate_hz": 0.0,
    }
    if total <= 0.0:
        return Classification("unknown", 0.0, feats)

    # noise floor: a low quantile of the Welch PSD, corrected by the known Gamma(navg) spread
    psd, navg = _overlapped_psd(x, nfft)
    kth = int(FLOOR_PERCENTILE * (nfft - 1))
    quantile = float(scipy.special.gammaincinv(navg, FLOOR_PERCENTILE)) / navg
    noise = float(np.partition(psd, kth)[kth]) / quantile
    if noise <= 0.0:
        return Classification("unknown", 0.0, feats)

    # strongest line of the full-resolution spectrum; its power follows from Parseval
    window = _hann(n)
    full = scipy.fft.fft(x * window)
    full_p = (full.real**2 + full.imag**2) / float((window * window).sum())
    k_line = int(np.argmax(full_p))
    line_peak = float(full_p[k_line]) / noise
    lobe = (k_line + np.arange(-2, 3)) % n
    line_power = max(float((full_p[lobe] - noise).sum()) / n, 0.0)
    line_found = line_peak >= math.log(n) + _LINE_MARGIN
    feats["line_snr_db"] = _db(line_peak)

    band = _occupied_band(psd, noise)
    if band is None:
        if not line_found:
            return Classification("unknown", 0.0, feats)
        # a line too weak to lift the Welch PSD: a bare carrier
        above_half = full_p >= 0.5 * full_p[k_line]
        feats["occupied_bw_hz"] = float(np.count_nonzero(above_half[lobe])) * fs / n
        feats["center_offset_hz"] = float(scipy.fft.fftfreq(n, 1.0 / fs)[k_line])
        feats["snr_db"] = _db(line_power / (noise * lobe.size / n))
        feats["line_share"] = 1.0
        feats["envelope_kurtosis"] = 1.0
        feats["spectral_flatness"] = 0.0
        margin = _db(line_peak / (math.log(n) + _LINE_MARGIN))
        return Classification("cw", _clip01(0.6 + margin / 25.0), feats)

    k_lo, k_hi, r_lo, r_hi = band
    bin_hz = fs / nfft
    width_bins = k_hi - k_lo + 1
    occupied_bw = width_bins * bin_hz
    feats["occupied_bw_hz"] = occupied_bw
    feats["center_offset_hz"] = (0.5 * (k_lo + k_hi) - nfft // 2) * bin_hz
    in_region = psd[r_lo : r_hi + 1]
    estimator_bias = math.exp(float(scipy.special.digamma(navg))) / navg
    feats["spectral_flatness"] = _clip01(
        float(np.exp(np.mean(np.log(np.maximum(in_region, _TINY)))) / in_region.mean()) / estimator_bias
    )

    # restrict the capture to the occupied band (with a margin for roll-off skirts)
    pad = max(2, int(round(0.15 * (r_hi - r_lo + 1))))
    f1 = (min(k_lo, r_lo) - pad - nfft // 2 - 0.5) * bin_hz
    f2 = (max(k_hi, r_hi) + pad - nfft // 2 + 0.5) * bin_hz
    freqs = scipy.fft.fftfreq(n, 1.0 / fs)
    passed = (freqs >= f1) & (freqs <= f2)
    xb = scipy.fft.ifft(np.where(passed, scipy.fft.fft(x), 0.0))
    noise_b = noise * float(np.count_nonzero(passed)) / n
    pb = xb.real**2 + xb.imag**2
    total_b = float(pb.mean())
    signal_b = total_b - noise_b
    if signal_b <= _MIN_SIGNAL_OVER_NOISE * noise_b:
        return Classification("unknown", 0.0, feats)
    snr_db = _db(signal_b / noise_b)
    feats["snr_db"] = snr_db
    feats["line_share"] = _clip01(line_power / signal_b)
    kurt_total = float((pb * pb).mean()) / (total_b * total_b)
    kurtosis = (kurt_total * total_b * total_b - 4.0 * signal_b * noise_b - 2.0 * noise_b * noise_b) / (
        signal_b * signal_b
    )
    feats["envelope_kurtosis"] = float(min(max(kurtosis, 0.0), 1e3))
    quality = _clip01(0.3 + 0.7 * (snr_db + 3.0) / 13.0)  # full weight from 10 dB in-band SNR

    # cw: one full-resolution line holds the signal power
    if line_found and feats["line_share"] >= _LINE_SHARE:
        above_half = full_p >= 0.5 * full_p[k_line]
        feats["occupied_bw_hz"] = float(np.count_nonzero(above_half[lobe])) * fs / n
        feats["center_offset_hz"] = float(freqs[k_line])
        feats["snr_db"] = _db(line_power / (noise * lobe.size / n))
        return Classification("cw", _clip01(0.5 + 2.5 * (feats["line_share"] - _LINE_SHARE)) * quality, feats)

    # swept: instantaneously narrow, moving steadily
    concentration, linearity, sweep_rate = _sweep_features(xb, fs)
    feats["tf_concentration"] = float(concentration)
    feats["if_linearity"] = float(linearity)
    fm_like = concentration >= _SWEEP_CONCENTRATION
    if fm_like and linearity >= _SWEEP_LINEARITY:
        feats["sweep_rate_hz_per_s"] = float(sweep_rate)
        score = 0.5 + 0.25 * min((concentration - _SWEEP_CONCENTRATION) / _SWEEP_CONCENTRATION, 1.0)
        score += 0.25 * (linearity - _SWEEP_LINEARITY) / (1.0 - _SWEEP_LINEARITY)
        return Classification("swept", _clip01(score * quality), feats)

    # pulsed: the smoothed envelope has an off level and an on level and little between.
    # Tried on the band-limited capture (best SNR), then on the whole capture band,
    # where the off level is steadier, with the long and with a short smoothing.
    smooth = int(min(max(round(_COHERENCE_TIMES * nfft / width_bins), _PULSE_MIN_SMOOTH), n // 64))
    for i, (envelope, length) in enumerate(((pb, smooth), (power, smooth), (power, _PULSE_MIN_SMOOTH))):
        contrast, low, mid, high, duty = _envelope_levels(envelope, length)
        if i == 0:
            feats["envelope_contrast_db"] = contrast
        if (
            contrast >= _PULSE_CONTRAST_DB
            and low >= _PULSE_MIN_SHARE
            and high >= _PULSE_MIN_SHARE
            and mid <= 0.5 * min(low, high)
        ):
            feats["envelope_contrast_db"] = contrast
            feats["duty_cycle"] = _clip01(duty)
            valley = 1.0 - mid / (0.5 * min(low, high))
            return Classification("pulsed", _clip01((0.6 + 0.4 * valley) * quality), feats)

    # modulated: a symbol-rate line, or an envelope that is not Gaussian
    line_db, f_symbol = _symbol_line(
        pb, fs, _SYMBOL_BAND[0] * occupied_bw, min(_SYMBOL_BAND[1] * occupied_bw, 0.49 * fs)
    )
    feats["symbol_line_db"] = line_db
    if line_db > 0.0:
        # |x|^2 is sampled at fs too: a symbol rate above fs / 2 shows up at fs - rate.
        # The half-power width of a shaped linear modulation is its symbol rate.
        alias = fs - f_symbol
        feats["symbol_rate_hz"] = float(f_symbol if abs(f_symbol - occupied_bw) <= abs(alias - occupied_bw) else alias)
    # The kurtosis of n_ind independent Gaussian samples scatters by 2 / sqrt(n_ind).
    slack = min(max(_KURTOSIS_SLACK, 6.0 / math.sqrt(n * width_bins / nfft)), 3.0 * _KURTOSIS_SLACK)
    sub_gaussian = kurtosis < 2.0 - slack
    if line_db > 0.0 or sub_gaussian or fm_like:
        score = 0.45
        if line_db > 0.0:
            score += 0.3 * min(line_db / 6.0, 1.0)
        if sub_gaussian:
            score += 0.25 * min((2.0 - slack - kurtosis) / 0.3, 1.0)
        return Classification("modulated", _clip01(score * quality), feats)

    # noise: Gaussian envelope statistics and nothing periodic
    if kurtosis <= 2.0 + 1.5 * slack:
        edge = min(kurtosis - (2.0 - slack), 2.0 + 1.5 * slack - kurtosis) / slack
        return Classification("noise", _clip01((0.6 + 0.4 * min(edge, 1.0)) * quality), feats)
    return Classification("unknown", 0.0, feats)
