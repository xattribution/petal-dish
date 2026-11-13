"""Tests for core/detect.py: SignalTracker and classify (MATH.md section 7).

Every input is made here. PSD frames are a known mean spectrum times an independent
chi-square(2 navg) / (2 navg) draw per bin (what a Welch estimate of navg segments
gives); a few tests go through real IQ and `welch_psd_db` instead. Expected values
come from the injected parameters or from a brute-force recomputation, never from the
detector's own output.
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import numpy as np
import pytest
from scipy.special import gammaincinv

from petal_locate.core.detect import (
    CLASS_LABELS,
    Classification,
    Detection,
    SignalTracker,
    classify,
)
from petal_locate.core.spectrum import welch_psd_db

# ----------------------------------------------------------------------------- set-up

NFFT = 1024
NAVG = 8
SPAN_HZ = 31_250.0
CENTER_HZ = 260.0e6
BIN_HZ = SPAN_HZ / NFFT
RATE_HZ = 2.0
DT_NS = 500_000_000

DEFAULTS = dict(  # ARCHITECTURE.md section 3, processing.detection
    threshold_db=6.0,
    confirm_m=3,
    confirm_n=5,
    release_frames=6,
    merge_bins=2,
    baseline_tau_s=600.0,
    power_anomaly_db=6.0,
)

# One 25 kHz transponder whose relayed noise lifts the floor by 1.6 dB: bins 103..921.
PASSBAND = (CENTER_HZ - 12_500.0, CENTER_HZ + 12_500.0, 1.6)
# A passband wider than the window: no guard bins at all.
WIDE_PASSBAND = (CENTER_HZ - 1.0e6, CENTER_HZ + 1.0e6, 1.6)

MEDIAN_DB = 10.0 * np.log10(gammaincinv(NAVG, 0.5) / NAVG)  # median of a noise bin, dB re its mean


def cfg(**overrides: float) -> SimpleNamespace:
    return SimpleNamespace(**{**DEFAULTS, **overrides})


def bin_freq(k: float) -> float:
    """Centre frequency of bin k, downlink Hz."""
    return CENTER_HZ + (k - NFFT // 2) * BIN_HZ


def floor_lin(passbands: list[tuple[float, float, float]]) -> np.ndarray:
    """Mean linear PSD with no signal: thermal noise 1 plus the relayed noise of each passband."""
    freq = bin_freq(np.arange(NFFT))
    lin = np.ones(NFFT)
    for f_lo, f_hi, rise_db in passbands:
        lin[(freq >= f_lo) & (freq <= f_hi)] += 10.0 ** (rise_db / 10.0) - 1.0
    return lin


def with_block(lin: np.ndarray, k0: int, width: int, db_above: float) -> np.ndarray:
    """Copy of `lin` with bins k0 .. k0 + width - 1 raised to `db_above` dB above it."""
    out = lin.copy()
    out[k0 : k0 + width] *= 10.0 ** (db_above / 10.0)
    return out


def tone_shape(position: float) -> np.ndarray:
    """Share of a tone's power in each bin of a Hann-windowed frame (sums to 1).

    position: tone frequency in (fractional) bins of the fft-shifted frame.
    """
    n = np.arange(NFFT)
    window = np.hanning(NFFT + 1)[:-1]
    spec = np.fft.fftshift(np.fft.fft(window * np.exp(2j * np.pi * (position - NFFT // 2) * n / NFFT)))
    power = np.abs(spec) ** 2
    return power / power.sum()


def bins_of(det: Detection) -> tuple[int, int]:
    """First and last bin of a detection, from its edge frequencies."""
    lo = (det.f_lo_hz - CENTER_HZ) / BIN_HZ + NFFT // 2 + 0.5
    hi = (det.f_hi_hz - CENTER_HZ) / BIN_HZ + NFFT // 2 - 0.5
    assert abs(lo - round(lo)) < 1e-6 and abs(hi - round(hi)) < 1e-6
    return int(round(lo)), int(round(hi))


class Bench:
    """Feeds synthetic frames to a tracker and remembers them."""

    def __init__(self, tracker: SignalTracker, seed: int, navg: int = NAVG, dt_ns: int = DT_NS) -> None:
        self.tracker = tracker
        self.rng = np.random.default_rng(seed)
        self.navg = navg
        self.dt_ns = dt_ns
        self.i = 0
        self.frames: list[np.ndarray] = []

    @property
    def t_ns(self) -> int:
        """Time of the next frame."""
        return 1_800_000_000_000_000_000 + self.i * self.dt_ns

    def step(self, mean_lin: np.ndarray) -> list[Detection]:
        fluct = self.rng.chisquare(2 * self.navg, mean_lin.size) / (2 * self.navg)
        psd = (10.0 * np.log10(mean_lin * fluct)).astype(np.float32)
        self.frames.append(psd)
        dets = self.tracker.update(psd, self.t_ns)
        self.i += 1
        return dets

    def run(self, mean_lin: np.ndarray, count: int) -> list[list[Detection]]:
        return [self.step(mean_lin) for _ in range(count)]


def make_tracker(
    passbands: list[tuple[float, float, float]] | None = None,
    carriers: list[tuple[str, float, float]] | None = None,
    rate_hz: float = RATE_HZ,
    **overrides: float,
) -> SignalTracker:
    return SignalTracker(
        CENTER_HZ,
        SPAN_HZ,
        NFFT,
        rate_hz,
        cfg(**overrides),
        [PASSBAND] if passbands is None else passbands,
        [] if carriers is None else carriers,
    )


# ------------------------------------------------------------------- false alarm rate


@pytest.mark.parametrize(
    "passbands",
    [[PASSBAND], [], [WIDE_PASSBAND]],
    ids=["guard bins and passband", "no passband", "no guard bins"],
)
def test_no_false_tracks_over_2000_noise_frames(passbands):
    bench = Bench(make_tracker(passbands), seed=11)
    lin = floor_lin(passbands)
    for _ in range(2000):
        assert bench.step(lin) == []
    # 2000 frames is 1000 s: the baseline is the learned one by now and sits on the noise median
    # (median of 120 entries: 0.18 dB of scatter per bin)
    truth_db = 10.0 * np.log10(lin) + MEDIAN_DB
    assert np.max(np.abs(bench.tracker.baseline_db - truth_db)) < 1.0
    assert abs(float(np.mean(bench.tracker.baseline_db - truth_db))) < 0.05


# -------------------------------------------------------------------------- baseline


def test_baseline_is_nan_before_the_first_frame_and_float32_after():
    tracker = make_tracker()
    assert tracker.baseline_db.shape == (NFFT,) and np.isnan(tracker.baseline_db).all()
    Bench(tracker, seed=1).step(floor_lin([PASSBAND]))
    assert tracker.baseline_db.dtype == np.float32 and np.isfinite(tracker.baseline_db).all()


def test_prior_is_guard_median_smoothed_over_five_frames_plus_relay_rise():
    bench = Bench(make_tracker(threshold_db=40.0), seed=2)
    lin = floor_lin([PASSBAND])
    freq = bin_freq(np.arange(NFFT))
    in_passband = (freq >= PASSBAND[0]) & (freq <= PASSBAND[1])
    assert np.count_nonzero(~in_passband) == 205
    guard_medians: list[float] = []
    # baseline_tau_s / 10 = 60 s = 120 frame periods: the prior is in use up to frame 120
    for i in range(121):
        bench.step(lin)
        guard_medians.append(float(np.median(bench.frames[-1][~in_passband].astype(np.float64))))
        expected = np.mean(guard_medians[-5:]) + np.where(in_passband, PASSBAND[2], 0.0)
        np.testing.assert_allclose(bench.tracker.baseline_db, expected, atol=2e-5, err_msg=f"frame {i}")
    bench.step(lin)  # frame 121: the history now spans 60 s and takes over
    assert np.max(np.abs(bench.tracker.baseline_db - expected)) > 0.3


def test_overlapping_passbands_add_their_relayed_noise_linearly():
    second = (CENTER_HZ - 3_000.0, CENTER_HZ + 20_000.0, 3.0)
    tracker = make_tracker([PASSBAND, second], threshold_db=40.0)
    bench = Bench(tracker, seed=3)
    lin = floor_lin([PASSBAND, second])
    bench.run(lin, 5)
    freq = bin_freq(np.arange(NFFT))
    both = (freq >= second[0]) & (freq <= PASSBAND[1])
    only_first = (freq >= PASSBAND[0]) & (freq < second[0])
    base = bench.tracker.baseline_db.astype(np.float64)
    rise_both = 10.0 * np.log10(10.0 ** 0.16 + 10.0 ** 0.3 - 1.0)  # 1 + (10^.16 - 1) + (10^.3 - 1)
    assert abs(float(np.mean(base[both]) - np.mean(base[only_first])) - (rise_both - 1.6)) < 1e-4


def test_learned_baseline_is_median_of_one_frame_kept_every_five_seconds():
    """Default schedule: baseline_tau_s / 120 = 5 s, i.e. every 10th frame at 2 Hz, at most 120 kept."""
    bench = Bench(make_tracker(threshold_db=40.0), seed=4)  # threshold out of reach: nothing is frozen
    lin = floor_lin([PASSBAND])
    for i in range(1350):
        bench.step(lin)
        if i in (121, 125, 130, 131, 600, 1199, 1200, 1201, 1349):
            kept = np.array(bench.frames[0:i:10][-120:], dtype=np.float64)  # frames before i
            assert kept.shape[0] == min((i - 1) // 10 + 1, 120)
            np.testing.assert_allclose(
                bench.tracker.baseline_db, np.median(kept, axis=0), atol=2e-5, err_msg=f"frame {i}"
            )


def test_history_keep_interval_follows_frame_time_not_frame_count():
    """Frames arriving at 1 Hz instead of the nominal 2 Hz: still one kept every 5 s (every 5th)."""
    bench = Bench(make_tracker(threshold_db=40.0), seed=5, dt_ns=1_000_000_000)
    lin = floor_lin([PASSBAND])
    bench.run(lin, 200)
    kept = np.array(bench.frames[0:199:5], dtype=np.float64)
    np.testing.assert_allclose(bench.tracker.baseline_db, np.median(kept, axis=0), atol=2e-5)


def test_short_tau_keeps_every_frame_and_spans_tau():
    """baseline_tau_s = 60 s at 2 Hz: keep interval 0.5 s, 120 entries, history used after 6 s."""
    bench = Bench(make_tracker(threshold_db=40.0, baseline_tau_s=60.0), seed=6)
    lin = floor_lin([PASSBAND])
    for i in range(300):
        bench.step(lin)
        if i >= 13:  # entries of frames 0..12 span 6 s
            kept = np.array(bench.frames[max(0, i - 120) : i], dtype=np.float64)
            np.testing.assert_allclose(
                bench.tracker.baseline_db, np.median(kept, axis=0), atol=2e-5, err_msg=f"frame {i}"
            )


def test_slow_frames_shrink_the_history_to_tau():
    """0.1 Hz frames, tau 600 s: one frame per 10 s is all there is, so 60 entries span tau."""
    tracker = make_tracker(threshold_db=40.0, rate_hz=0.1)
    bench = Bench(tracker, seed=7, dt_ns=10_000_000_000)
    lin = floor_lin([PASSBAND])
    bench.run(lin, 100)
    kept = np.array(bench.frames[99 - 60 : 99], dtype=np.float64)
    np.testing.assert_allclose(tracker.baseline_db, np.median(kept, axis=0), atol=2e-5)


@pytest.mark.parametrize("navg", [1, 2, 4, 8, 32])
def test_floor_without_guard_bins_sits_on_the_noise_median_for_any_navg(navg):
    """The 10th percentile alone is far below the median; the corrected floor is not.

    This is the deviation from MATH.md section 7 ("the 10th percentile of all bins"):
    the start-up threshold would otherwise be lower by the figures asserted here.
    """
    tracker = make_tracker([WIDE_PASSBAND], threshold_db=40.0)
    bench = Bench(tracker, seed=8 + navg, navg=navg)
    lin = floor_lin([WIDE_PASSBAND])
    median_db = 10.0 * np.log10(gammaincinv(navg, 0.5) / navg)
    errors, raw = [], []
    for _ in range(40):
        bench.step(lin)
        truth = 10.0 * np.log10(lin) + median_db  # prior F + R_k should equal this in every bin
        errors.append(float(np.mean(tracker.baseline_db - truth)))
        raw.append(float(np.percentile(bench.frames[-1] - 10.0 * np.log10(lin), 10.0)) - median_db)
    # one frame's estimate scatters by 0.77 dB at navg = 1 and 0.10 dB at navg = 8
    assert abs(np.mean(errors)) < (0.5 if navg == 1 else 0.15), np.mean(errors)
    assert np.max(np.abs(errors[4:])) < (1.5 if navg == 1 else 0.4)
    expected_gap = 10.0 * np.log10(gammaincinv(navg, 0.1) / gammaincinv(navg, 0.5))
    assert abs(np.mean(raw) - expected_gap) < 0.3
    if navg <= 8:
        assert np.mean(raw) < -2.0  # -2.2 dB at navg = 8, -8.2 dB at navg = 1


def test_floor_without_guard_bins_ignores_a_strong_emitter_over_half_the_window():
    """Low-percentile floor: 55 % of the bins 20 dB up from the first frame, floor still found."""
    tracker = make_tracker([WIDE_PASSBAND])
    bench = Bench(tracker, seed=9)
    lin = floor_lin([WIDE_PASSBAND])
    sig = with_block(lin, 200, 563, 20.0)
    quiet = np.r_[0:195, 770:NFFT]
    truth = 10.0 * np.log10(lin[quiet]) + MEDIAN_DB
    history, errors = [], []
    for _ in range(30):
        history.append(bench.step(sig))
        errors.append(float(np.mean(tracker.baseline_db[quiet] - truth)))
    # In the first frame 10 % of all bins is 22 % of the quiet ones: the floor reads high
    # by the 22nd-to-10th percentile distance of the noise (0.95 dB at navg = 8), never
    # low. From then on the emitter's bins are in detection and left out.
    assert 0.4 < errors[0] < 1.5
    assert np.max(np.abs(errors[5:])) < 0.3
    assert [len(d) for d in history] == [0, 0] + [1] * 28
    assert bins_of(history[-1][0]) == (200, 762)


@pytest.mark.parametrize("passbands", [[PASSBAND], [WIDE_PASSBAND]], ids=["guard bins", "no guard bins"])
def test_emitter_over_the_whole_window_during_startup_does_not_lift_its_own_prior(passbands):
    """12 dB over every bin, guard bins included, 10 s after start: F is held, not dragged up.

    The frame-by-frame guard median (or percentile) alone would follow the emitter
    within five frames and hide it; bins in detection are left out of F as they are
    left out of the history.
    """
    tracker = make_tracker(passbands)
    bench = Bench(tracker, seed=12)
    lin = floor_lin(passbands)
    truth = 10.0 * np.log10(lin) + MEDIAN_DB
    bench.run(lin, 20)
    jammed = bench.run(lin * 10.0 ** 1.2, 40)
    assert [len(d) for d in jammed] == [0, 0] + [1] * 38
    for dets in jammed[2:]:
        assert bins_of(dets[0]) == (0, NFFT - 1) and dets[0].track_id == 1
        assert abs(dets[0].snr_db - 12.0) < 0.6
    assert np.max(np.abs(tracker.baseline_db - truth)) < 0.35
    literal = float(np.median(np.array(bench.frames[-5:], dtype=np.float64) - 10.0 * np.log10(lin)))
    assert literal - MEDIAN_DB > 11.0  # where the unguarded estimate would be by now
    after = bench.run(lin, 30)
    assert [len(d) for d in after] == [1] * 5 + [0] * 25
    assert np.max(np.abs(tracker.baseline_db - truth)) < 0.35


@pytest.mark.parametrize(
    ("passbands", "first", "width"),
    [([PASSBAND], 26, 972), ([WIDE_PASSBAND], 15, 994)],
    ids=["guard bins: three quarters of them covered", "no guard bins: 97 % covered"],
)
def test_signal_on_from_the_first_frame_over_most_of_the_window_is_detected_at_once(passbands, first, width):
    """K1: a signal already on when the tracker starts, covering the guard bins' median
    (or nine tenths of a window without guard bins), lifted the floor with it and was
    learned as noise for good. The floor now steps down to a lower quantile."""
    tracker = make_tracker(passbands)
    bench = Bench(tracker, seed=31)
    lin = floor_lin(passbands)
    sig = with_block(lin, first, width, 10.0)
    history = bench.run(sig, 30)
    assert [len(d) for d in history] == [0, 0] + [1] * 28
    lo, hi = bins_of(history[-1][0])
    assert abs(lo - first) <= 1 and abs(hi - (first + width - 1)) <= 1
    assert abs(history[-1][0].snr_db - 10.0) < 0.6
    free = np.r_[0 : first - 2, first + width + 2 : NFFT]
    truth = 10.0 * np.log10(lin[free]) + MEDIAN_DB
    assert abs(float(np.mean(tracker.baseline_db[free] - truth))) < 0.3
    assert abs(tracker.floor_db - MEDIAN_DB) < 0.3


def test_signal_filling_the_whole_window_from_the_first_frame_shows_in_floor_db():
    """Nothing inside the window tells such a signal from noise; floor_db carries it, so
    the caller can compare the window with the receiver's other windows."""
    for passbands in ([PASSBAND], [WIDE_PASSBAND]):
        tracker = make_tracker(passbands)
        assert np.isnan(tracker.floor_db) and tracker.floor_from_guard == (passbands == [PASSBAND])
        bench = Bench(tracker, seed=32)
        history = bench.run(floor_lin(passbands) * 10.0 ** 1.2, 20)
        assert all(d == [] for d in history)
        assert abs(tracker.floor_db - (12.0 + MEDIAN_DB)) < 0.4


def test_learning_lasts_a_tenth_of_baseline_tau_and_reset_time_accepts_a_clock_step():
    tracker = make_tracker()
    bench = Bench(tracker, seed=33)
    lin = floor_lin([PASSBAND])
    sig = with_block(lin, 400, 40, 10.0)
    bench.run(lin, 60)
    assert tracker.learning  # 30 s of 600 s
    bench.run(lin, 70)
    assert not tracker.learning  # the history spans 60 s
    assert [len(d) for d in bench.run(sig, 5)][-1] == 1
    baseline = tracker.baseline_db.copy()
    psd = bench.frames[-1]
    step_back = bench.t_ns - 3_600 * 10**9  # the frame clock is corrected an hour back
    with pytest.raises(ValueError, match="increase"):
        tracker.update(psd, step_back)
    tracker.reset_time()
    dets = tracker.update(psd, step_back)
    assert [d.track_id for d in dets] == [1]  # the track and the baseline go on
    np.testing.assert_allclose(tracker.baseline_db, baseline, atol=1e-6)
    with pytest.raises(ValueError, match="increase"):
        tracker.update(psd, step_back)


def test_track_under_a_passing_full_window_emitter_keeps_its_id():
    bench = Bench(make_tracker(), seed=13)
    lin = floor_lin([PASSBAND])
    sig = with_block(lin, 300, 30, 8.0)
    bench.run(sig, 300)
    during = bench.run(sig * 10.0 ** 1.5, 30)
    assert all([(d.track_id, bins_of(d)) for d in dets] == [(1, (0, NFFT - 1))] for dets in during)
    after = bench.run(sig, 30)
    assert all([d.track_id for d in dets] == [1] for dets in after)
    assert all(bins_of(dets[0]) == (300, 329) for dets in after[DEFAULTS["confirm_n"] :])
    assert abs(after[-1][0].snr_db - 8.0) < 1.5


# ------------------------------------------------- confirmation, release, measurements


def test_signal_8db_above_floor_is_confirmed_within_confirm_n_and_released():
    tracker = make_tracker()
    bench = Bench(tracker, seed=20)
    lin = floor_lin([PASSBAND])
    sig = with_block(lin, 300, 12, 8.0)
    assert all(d == [] for d in bench.run(lin, 150))  # 75 s: baseline learned
    t_on = bench.t_ns
    on = bench.run(sig, 40)
    first = next(i for i, d in enumerate(on) if d)
    assert first == DEFAULTS["confirm_m"] - 1 < DEFAULTS["confirm_n"]
    for i, dets in enumerate(on[first:], start=first):
        assert len(dets) == 1
        det = dets[0]
        assert det.track_id == 1 and det.first_seen_ns == t_on
        assert det.last_seen_ns == t_on + i * DT_NS
        assert det.authorized_carrier_id is None and det.anomaly is False
        lo, hi = bins_of(det)
        assert 300 <= lo <= 301 and 310 <= hi <= 311
        assert abs(det.snr_db - 8.0) < 2.0
    assert bins_of(on[-1][0]) == (300, 311)
    assert on[-1][0].f_lo_hz == pytest.approx(bin_freq(300) - BIN_HZ / 2, abs=1e-3)
    assert on[-1][0].f_hi_hz == pytest.approx(bin_freq(311) + BIN_HZ / 2, abs=1e-3)

    t_last = bench.t_ns - DT_NS
    off = bench.run(lin, 20)
    # release_frames = 6 misses: listed (with held values) for 5 frames, gone on the 6th
    assert [len(d) for d in off] == [1] * 5 + [0] * 15
    assert all(d[0].track_id == 1 and d[0].last_seen_ns == t_last for d in off[:5])
    assert off[4][0].snr_db == on[-1][0].snr_db


def test_signal_during_startup_is_confirmed_against_the_prior():
    bench = Bench(make_tracker(), seed=21)
    lin = floor_lin([PASSBAND])
    bench.run(lin, 8)
    on = bench.run(with_block(lin, 700, 12, 8.0), 10)
    assert [len(d) for d in on] == [0, 0] + [1] * 8
    assert bins_of(on[-1][0])[0] in (700, 701) and bins_of(on[-1][0])[1] in (710, 711)


def test_reported_power_snr_cn0_follow_their_definitions():
    """Brute force from the frame and the exposed baseline, over the reported extent."""
    tracker = make_tracker()
    bench = Bench(tracker, seed=22)
    lin = floor_lin([PASSBAND])
    bench.run(lin, 150)
    sig = with_block(lin, 500, 25, 10.0)
    for _ in range(30):
        dets = bench.step(sig)
        if not dets:
            continue
        det = dets[0]
        lo, hi = bins_of(det)
        p = 10.0 ** (bench.frames[-1].astype(np.float64) / 10.0)
        b = 10.0 ** (tracker.baseline_db.astype(np.float64) / 10.0)
        power_db = 10.0 * np.log10(p[lo : hi + 1].mean())
        assert det.power_db == pytest.approx(power_db, abs=1e-4)
        assert det.snr_db == pytest.approx(power_db - 10.0 * np.log10(b[lo : hi + 1].mean()), abs=1e-4)
        # sum over the extent and one main-lobe skirt bin on each side
        excess = np.maximum(p[lo - 1 : hi + 2] - b[lo - 1 : hi + 2], 0.0).sum()
        assert det.cn0_dbhz == pytest.approx(10.0 * np.log10(excess / b[lo : hi + 1].mean() * BIN_HZ), abs=1e-4)


@pytest.mark.parametrize(
    "pattern, first_listed",
    [
        ("100100100100100100100", None),  # never 3 in any 5 consecutive frames
        ("110001100011000110001", None),
        ("10101", 4),  # 3 of the last 5 on the fifth frame
        ("111", 2),
        ("0110100", 4),
    ],
)
def test_m_of_n_confirmation(pattern, first_listed):
    bench = Bench(make_tracker(), seed=23)
    lin = floor_lin([PASSBAND])
    sig = with_block(lin, 400, 8, 15.0)  # strong: seen in every frame it is present
    bench.run(lin, 150)
    t0 = bench.t_ns
    listed = [bool(bench.step(sig if c == "1" else lin)) for c in pattern]
    if first_listed is None:
        assert not any(listed)
    else:
        assert listed.index(True) == first_listed
        dets = bench.step(sig)
        assert dets[0].first_seen_ns == t0 + pattern.index("1") * DT_NS


@pytest.mark.parametrize("m, n", [(1, 1), (2, 2), (5, 5), (2, 6)])
def test_confirmation_honours_cfg(m, n):
    bench = Bench(make_tracker(confirm_m=m, confirm_n=n), seed=24)
    lin = floor_lin([PASSBAND])
    bench.run(lin, 150)
    listed = [len(d) for d in bench.run(with_block(lin, 400, 8, 15.0), m + 2)]
    assert listed == [0] * (m - 1) + [1] * 3


@pytest.mark.parametrize("release_frames", [1, 3, 6])
def test_release_after_release_frames_misses(release_frames):
    bench = Bench(make_tracker(release_frames=release_frames), seed=25)
    lin = floor_lin([PASSBAND])
    sig = with_block(lin, 400, 8, 15.0)
    bench.run(lin, 150)
    bench.run(sig, 10)
    assert [len(d) for d in bench.run(lin, release_frames + 2)] == [1] * (release_frames - 1) + [0] * 3


def test_track_id_is_stable_across_frames_and_new_after_release():
    bench = Bench(make_tracker(), seed=26)
    lin = floor_lin([PASSBAND])
    sig = with_block(lin, 400, 8, 12.0)
    bench.run(lin, 150)
    ids = {d.track_id for dets in bench.run(sig, 100) for d in dets}
    assert ids == {1}
    # a gap shorter than release_frames keeps the track ...
    ids |= {d.track_id for dets in bench.run(lin, 5) for d in dets}
    ids |= {d.track_id for dets in bench.run(sig, 20) for d in dets}
    assert ids == {1}
    # ... a released track is gone for good: the same emitter comes back under a new id
    assert bench.run(lin, 10)[-1] == []
    t_back = bench.t_ns
    again = bench.run(sig, 20)
    assert {d.track_id for dets in again for d in dets} == {2}
    assert again[-1][0].first_seen_ns == t_back


def test_two_emitters_keep_distinct_ids_when_one_leaves():
    bench = Bench(make_tracker(), seed=27)
    lin = floor_lin([PASSBAND])
    both = with_block(with_block(lin, 300, 10, 12.0), 600, 10, 12.0)
    bench.run(lin, 150)
    dets = bench.run(both, 30)[-1]
    assert [bins_of(d) for d in dets] == [(300, 309), (600, 609)]  # ordered by frequency
    low_id, high_id = dets[0].track_id, dets[1].track_id
    assert {low_id, high_id} == {1, 2}
    only_high = bench.run(with_block(lin, 600, 10, 12.0), 30)[-1]
    assert [(d.track_id, bins_of(d)) for d in only_high] == [(high_id, (600, 609))]


# ------------------------------------------------------------- start-up emitter, drift


@pytest.mark.parametrize("passbands", [[PASSBAND], [WIDE_PASSBAND]], ids=["guard bins", "no guard bins"])
def test_emitter_on_from_first_frame_is_detected_against_prior_and_never_absorbed(passbands):
    tracker = make_tracker(passbands)
    bench = Bench(tracker, seed=30)
    lin = floor_lin(passbands)
    sig = with_block(lin, 300, 30, 8.0)
    frames = int(DEFAULTS["baseline_tau_s"] * RATE_HZ) + 200  # 700 s > baseline_tau_s
    t0 = bench.t_ns
    history = bench.run(sig, frames)
    assert [len(d) for d in history[:3]] == [0, 0, 1]
    exact = 0
    for dets in history[2:]:
        assert len(dets) == 1 and dets[0].track_id == 1 and dets[0].first_seen_ns == t0
        lo, hi = bins_of(dets[0])
        # (a noise bin next to the emitter crosses the threshold about once in 10^6 frames)
        assert 299 <= lo <= 301 and 328 <= hi <= 330
        exact += (lo, hi) == (300, 329)
        assert abs(dets[0].snr_db - 8.0) < 1.5
    assert exact > 0.98 * (frames - 2)
    # the emitter's bins (and their one-bin skirts) still carry the prior F + R_k ...
    truth = 10.0 * np.log10(lin) + MEDIAN_DB
    base = tracker.baseline_db
    assert np.max(np.abs(base[299:331] - truth[299:331])) < 0.3
    assert np.ptp(base[299:331]) < 1e-4  # flat: no bin of it was learned
    # ... while the rest of the window is the learned per-bin median (not flat)
    assert np.ptp(base[340:400]) > 0.2 and np.max(np.abs(base[340:400] - truth[340:400])) < 0.8

    # once it leaves it is released and its bins start learning like any others
    assert [len(d) for d in bench.run(lin, 8)] == [1] * 5 + [0] * 3
    bench.run(lin, 140)
    assert np.ptp(tracker.baseline_db[299:331]) > 0.2
    # (a median of 14 entries so far: 0.5 dB of scatter per bin)
    assert np.max(np.abs(tracker.baseline_db[299:331] - truth[299:331])) < 2.0
    assert abs(float(np.mean(tracker.baseline_db[299:331] - truth[299:331]))) < 0.4


def test_slow_2db_drift_of_the_whole_floor_creates_no_tracks():
    tracker = make_tracker()
    bench = Bench(tracker, seed=31)
    lin = floor_lin([PASSBAND])
    for i in range(2000):
        drift_db = 2.0 * i / 1999.0
        assert bench.step(lin * 10.0 ** (drift_db / 10.0)) == []
    # the learned baseline follows, lagging by about half the history (300 s of 1000 s)
    lag_db = float(np.mean(10.0 * np.log10(lin) + 2.0 + MEDIAN_DB - tracker.baseline_db))
    assert 0.3 < lag_db < 0.9


def test_frozen_prior_follows_floor_drift_under_a_startup_emitter():
    """The prior of bins frozen from the start is F + R_k with the live F, so it drifts with the floor."""
    bench = Bench(make_tracker(), seed=32)
    lin = floor_lin([PASSBAND])
    sig = with_block(lin, 300, 30, 8.0)
    for i in range(1000):
        dets = bench.step(sig * 10.0 ** (2.0 * i / 999.0 / 10.0))
        if i >= 2:
            assert len(dets) == 1 and dets[0].track_id == 1
            assert abs(dets[0].snr_db - 8.0) < 1.5


def test_bins_in_detection_are_frozen_and_the_rest_keep_learning():
    """Short history (60 s) and an emitter that stays 200 s: its bins keep the pre-emitter median."""
    tracker = make_tracker(baseline_tau_s=60.0)
    bench = Bench(tracker, seed=33)
    lin = floor_lin([PASSBAND])
    bench.run(lin, 40)
    before = np.median(np.array(bench.frames[:40], dtype=np.float64), axis=0)
    history = bench.run(with_block(lin, 500, 20, 10.0), 400)
    assert all(len(d) == 1 and d[0].track_id == 1 for d in history[2:])
    base = tracker.baseline_db.astype(np.float64)
    np.testing.assert_allclose(base[499:521], before[499:521], atol=2e-5)  # extent and one skirt bin
    recent = np.median(np.array(bench.frames[-121:-1], dtype=np.float64), axis=0)
    np.testing.assert_allclose(base[530:900], recent[530:900], atol=2e-5)
    assert np.max(np.abs(base[530:900] - before[530:900])) > 0.3
    # released: the bins resume from where they stopped
    bench.run(lin, 30)
    assert np.max(np.abs(tracker.baseline_db[499:521] - before[499:521])) > 0.05
    assert np.max(np.abs(tracker.baseline_db[499:521] - (10.0 * np.log10(lin[499:521]) + MEDIAN_DB))) < 1.0


# ----------------------------------------------------------------- authorised carriers

CARRIER = ("carrier-1", bin_freq(400) - BIN_HZ / 2, bin_freq(439) + BIN_HZ / 2)  # bins 400..439


def carrier_lin(lin: np.ndarray, rise_db: float) -> np.ndarray:
    """The authorised carrier, nominally 15 dB above the floor, `rise_db` above nominal."""
    return with_block(lin, 400, 40, 15.0 + rise_db)


def test_authorised_carrier_is_silent_until_power_rises_by_more_than_power_anomaly_db():
    tracker = make_tracker(carriers=[CARRIER])
    bench = Bench(tracker, seed=40)
    lin = floor_lin([PASSBAND])
    # on from the first frame, 15 dB above the floor: never tested against the prior
    assert all(d == [] for d in bench.run(carrier_lin(lin, 0.0), 300))
    # its baseline is its own level
    level = 10.0 * np.log10(lin[400:440]) + 15.0 + MEDIAN_DB
    assert np.max(np.abs(tracker.baseline_db[400:440] - level)) < 1.5  # median of 31 entries per bin
    assert abs(float(np.mean(tracker.baseline_db[400:440] - level))) < 0.3
    # 3 dB up: single bins cross the 6 dB threshold now and then, the carrier's power did not
    assert all(d == [] for d in bench.run(carrier_lin(lin, 3.0), 300))
    # 9 dB up: reported, as that carrier, as an anomaly
    t_rise = bench.t_ns
    risen = bench.run(carrier_lin(lin, 9.0), 60)
    first = next(i for i, d in enumerate(risen) if d)
    assert first < DEFAULTS["confirm_n"]
    for dets in risen[first:]:
        assert len(dets) == 1
        assert dets[0].authorized_carrier_id == "carrier-1" and dets[0].anomaly is True
        assert 400 <= bins_of(dets[0])[0] and bins_of(dets[0])[1] <= 439
    assert risen[-1][0].last_seen_ns == t_rise + 59 * DT_NS
    assert bins_of(risen[-1][0]) == (400, 439)
    # 9 dB above nominal; the baseline had begun to absorb the earlier 3 dB step
    assert 6.0 < risen[-1][0].snr_db < 9.6
    # back to nominal: released
    assert all(d == [] for d in bench.run(carrier_lin(lin, 0.0), 30)[DEFAULTS["release_frames"] :])


def test_power_anomaly_db_above_threshold_keeps_a_tracked_carrier_unreported():
    """threshold 6 dB, anomaly 10 dB: 8 dB up is tracked but not reported, 13 dB up is reported."""
    bench = Bench(make_tracker(carriers=[CARRIER], power_anomaly_db=10.0), seed=41)
    lin = floor_lin([PASSBAND])
    assert all(d == [] for d in bench.run(carrier_lin(lin, 0.0), 300))
    assert all(d == [] for d in bench.run(carrier_lin(lin, 8.0), 200))
    risen = bench.run(carrier_lin(lin, 13.0), 20)
    assert all(len(d) == 1 and d[0].anomaly and d[0].authorized_carrier_id == "carrier-1" for d in risen[5:])
    # back to 8 dB up: still above threshold, no longer an anomaly, so no longer listed
    settled = bench.run(carrier_lin(lin, 8.0), 40)
    assert all(d == [] for d in settled[DEFAULTS["release_frames"] + DEFAULTS["confirm_n"] :])


def test_carrier_bins_are_not_tested_until_their_history_spans_30_s():
    """A carrier that jumps 12 dB after 10 s is not flagged then (history too short), but is after 30 s."""
    lin = floor_lin([PASSBAND])
    early = Bench(make_tracker(carriers=[CARRIER]), seed=42)
    early.run(carrier_lin(lin, 0.0), 20)  # 10 s
    assert all(d == [] for d in early.run(carrier_lin(lin, 12.0), 30))  # up to 25 s
    late = Bench(make_tracker(carriers=[CARRIER]), seed=42)
    late.run(carrier_lin(lin, 0.0), 70)  # 35 s: entries at 0, 5, .., 30 s
    assert any(late.run(carrier_lin(lin, 12.0), 8))


def test_emitter_off_the_carrier_is_reported_while_the_carrier_stays_silent():
    bench = Bench(make_tracker(carriers=[CARRIER]), seed=43)
    lin = floor_lin([PASSBAND])
    bench.run(carrier_lin(lin, 0.0), 200)
    dets = bench.run(with_block(carrier_lin(lin, 0.0), 600, 10, 10.0), 20)[-1]
    assert len(dets) == 1 and bins_of(dets[0]) == (600, 609)
    assert dets[0].authorized_carrier_id is None and dets[0].anomaly is False


def test_run_must_overlap_the_carrier_by_half_its_own_width_to_carry_its_id():
    lin = floor_lin([PASSBAND])
    # a 25 dB emitter over bins 350..489 buries the carrier: 140 bins, 44 of them the carrier's
    wide = Bench(make_tracker(carriers=[CARRIER]), seed=44)
    wide.run(carrier_lin(lin, 0.0), 200)
    dets = wide.run(with_block(carrier_lin(lin, 0.0), 350, 140, 25.0), 20)[-1]
    assert len(dets) == 1 and bins_of(dets[0]) == (350, 489)
    assert dets[0].authorized_carrier_id is None and dets[0].anomaly is False
    # the same emitter over bins 395..444 lies mostly on the carrier
    narrow = Bench(make_tracker(carriers=[CARRIER]), seed=44)
    narrow.run(carrier_lin(lin, 0.0), 200)
    dets = narrow.run(with_block(carrier_lin(lin, 0.0), 395, 50, 25.0), 20)[-1]
    assert len(dets) == 1 and bins_of(dets[0]) == (395, 444)
    assert dets[0].authorized_carrier_id == "carrier-1" and dets[0].anomaly is True


def test_carrier_narrower_than_a_bin_and_carrier_outside_the_window():
    """A CW carrier between two bin centres still owns its main-lobe bins; one outside the window is ignored."""
    cw = ("beacon", bin_freq(700.4) - 1.0, bin_freq(700.4) + 1.0)
    outside = ("elsewhere", CENTER_HZ + 1.0e6, CENTER_HZ + 1.1e6)
    bench = Bench(make_tracker(carriers=[cw, outside]), seed=45)
    lin = floor_lin([PASSBAND])
    beacon = lin * (1.0 + 10.0 ** 4.5 / BIN_HZ * tone_shape(700.4))  # 45 dB-Hz, 28 dB over the floor
    assert all(d == [] for d in bench.run(beacon, 200))
    risen = lin * (1.0 + 10.0 ** 5.5 / BIN_HZ * tone_shape(700.4))
    dets = bench.run(risen, 10)[-1]
    assert len(dets) == 1 and dets[0].authorized_carrier_id == "beacon" and dets[0].anomaly


# --------------------------------------------------------------------------- merging


@pytest.mark.parametrize("merge_bins", [0, 2, 5])
def test_signals_within_merge_bins_merge_and_farther_apart_do_not(merge_bins):
    lin = floor_lin([PASSBAND])
    for gap in (merge_bins, merge_bins + 1):
        # bins 300..305 and, after `gap` quiet bins, six more
        merged, apart = [(300, 311 + gap)], [(300, 305), (306 + gap, 311 + gap)]
        expected = merged if gap <= merge_bins else apart
        bench = Bench(make_tracker(merge_bins=merge_bins), seed=50)
        bench.run(lin, 150)
        sig = with_block(with_block(lin, 300, 6, 15.0), 306 + gap, 6, 15.0)
        history = bench.run(sig, 60)
        for dets in history[2:]:
            assert [bins_of(d) for d in dets] == expected, (merge_bins, gap)
        assert len({d.track_id for dets in history for d in dets}) == len(expected)


def test_emitter_appearing_within_merge_bins_of_a_track_joins_it():
    bench = Bench(make_tracker(), seed=51)
    lin = floor_lin([PASSBAND])
    bench.run(lin, 150)
    one = with_block(lin, 300, 6, 15.0)
    assert bins_of(bench.run(one, 20)[-1][0]) == (300, 305)
    dets = bench.run(with_block(one, 308, 6, 15.0), 20)[-1]
    assert [(d.track_id, bins_of(d)) for d in dets] == [(1, (300, 313))]
    # and the extent shrinks back when the second one leaves
    assert [(d.track_id, bins_of(d)) for d in bench.run(one, 20)[-1]] == [(1, (300, 305))]


def test_wide_signal_near_threshold_is_one_stable_track():
    """200 bins 6.5 dB up: each bin is above threshold 6 frames in 10, yet it is one signal."""
    bench = Bench(make_tracker(), seed=52)
    lin = floor_lin([PASSBAND])
    bench.run(lin, 150)
    history = bench.run(with_block(lin, 400, 200, 6.5), 300)
    assert [len(d) for d in history[:2]] == [0, 0]
    # the first frames may show the fragments as two tracks until they have shared
    # a run for confirm_m frames
    assert all(1 <= len(dets) <= 3 for dets in history[2:10])
    ids = set()
    for dets in history[10:]:
        assert len(dets) == 1
        ids.add(dets[0].track_id)
        lo, hi = bins_of(dets[0])
        assert 399 <= lo <= 402 and 597 <= hi <= 600
    assert len(ids) == 1


def test_one_frame_broadband_burst_does_not_fuse_tracks_but_a_lasting_bridge_does():
    bench = Bench(make_tracker(), seed=53)
    lin = floor_lin([PASSBAND])
    two = with_block(with_block(lin, 200, 11, 15.0), 700, 11, 15.0)
    bench.run(lin, 150)
    before = bench.run(two, 20)[-1]
    assert [(d.track_id, bins_of(d)) for d in before] == [(1, (200, 210)), (2, (700, 710))]
    burst = bench.step(two * 10.0)  # the whole window 10 dB up for one frame
    assert [d.track_id for d in burst] == [1, 2]
    after = bench.run(two, 12)[-1]
    assert [(d.track_id, bins_of(d)) for d in after] == [(1, (200, 210)), (2, (700, 710))]
    # an emitter that covers both for good: one signal, under the older id
    bridged = bench.run(with_block(lin, 200, 511, 15.0), 12)
    assert [(d.track_id, bins_of(d)) for d in bridged[-1]] == [(1, (200, 710))]
    assert all({d.track_id for d in dets} <= {1, 2} for dets in bridged)


def test_merge_is_undone_with_the_old_ids_when_the_bridge_goes_away():
    """Two emitters, a broadband emitter over both for 10 s, then the two again."""
    bench = Bench(make_tracker(), seed=54)
    lin = floor_lin([PASSBAND])
    two = with_block(with_block(lin, 200, 11, 15.0), 700, 11, 15.0)
    bench.run(lin, 150)
    t_first = bench.t_ns
    bench.run(with_block(lin, 200, 11, 15.0), 10)
    t_second = bench.t_ns
    before = bench.run(two, 20)[-1]
    assert [(d.track_id, d.first_seen_ns) for d in before] == [(1, t_first), (2, t_second)]
    during = bench.run(with_block(two, 150, 650, 12.0), 20)
    assert [(d.track_id, bins_of(d), d.first_seen_ns) for d in during[-1]] == [(1, (150, 799), t_first)]
    after = bench.run(two, 30)
    # the 489 quiet bins between them have to stay quiet for release_frames + confirm_n frames
    patience = DEFAULTS["release_frames"] + DEFAULTS["confirm_n"]
    assert all([d.track_id for d in dets] == [1] for dets in after[: patience - 1])
    for dets in after[patience:]:
        assert [(d.track_id, bins_of(d), d.first_seen_ns) for d in dets] == [
            (1, (200, 210), t_first),
            (2, (700, 710), t_second),
        ]
    assert abs(after[-1][0].snr_db - 15.0) < 1.5 and abs(after[-1][1].snr_db - 15.0) < 1.5


def test_wide_track_that_becomes_two_signals_is_split():
    bench = Bench(make_tracker(), seed=55)
    lin = floor_lin([PASSBAND])
    bench.run(lin, 150)
    t_wide = bench.t_ns
    wide = bench.run(with_block(lin, 200, 511, 12.0), 20)[-1]
    assert [(d.track_id, bins_of(d)) for d in wide] == [(1, (200, 710))]
    two = with_block(with_block(lin, 200, 11, 12.0), 700, 11, 12.0)
    history = bench.run(two, 30)
    assert all(len(dets) == 1 for dets in history[:10])
    split_at = next(i for i, dets in enumerate(history) if len(dets) == 2)
    assert split_at <= DEFAULTS["release_frames"] + DEFAULTS["confirm_n"]
    for dets in history[split_at:]:
        assert [(d.track_id, bins_of(d)) for d in dets] == [(1, (200, 210)), (2, (700, 710))]
    # (a noise hit just before the emitter can be taken as its first frame)
    assert t_wide - DEFAULTS["confirm_n"] * DT_NS <= history[-1][0].first_seen_ns <= t_wide
    assert history[-1][1].first_seen_ns == bench.t_ns - (30 - split_at) * DT_NS  # new id: first seen at the split
    # the bins between them are out of detection again and learn the floor
    frozen = bench.tracker.baseline_db[300:600].copy()
    bench.run(two, 40)
    assert np.max(np.abs(bench.tracker.baseline_db[300:600] - frozen)) > 0.05


def test_stray_bin_in_the_gap_of_a_split_track_is_never_reported():
    """One noise-like spike between the two signals, shortly before the track is cut:
    the spike's bin forms a piece of its own, which has to earn confirmation and does not."""
    bench = Bench(make_tracker(), seed=56)
    lin = floor_lin([PASSBAND])
    bench.run(lin, 150)
    bench.run(with_block(lin, 200, 511, 12.0), 20)
    two = with_block(with_block(lin, 200, 11, 12.0), 700, 11, 12.0)
    history = bench.run(two, 4)
    history += bench.run(with_block(two, 530, 1, 12.0), 1)  # the spike
    history += bench.run(two, 35)
    allowed = {(200, 710), (200, 210), (700, 710)}
    assert all({bins_of(d) for d in dets} <= allowed for dets in history)
    assert [(d.track_id, bins_of(d)) for d in history[-1]] == [(1, (200, 210)), (2, (700, 710))]
    assert {d.track_id for dets in history for d in dets} == {1, 2}


# ----------------------------------------------------------------------------- C/N0


@pytest.mark.parametrize("cn0_dbhz", [24.0, 45.0])
@pytest.mark.parametrize("position", [50.0, 50.25, 50.5])
def test_cn0_of_a_tone_narrower_than_a_bin(cn0_dbhz, position):
    """24 dB-Hz puts a bin-centred tone's peak bin 8 dB above the floor (the weakest that confirms).

    The noise density under so narrow a signal is read from the baseline of its own
    one to three bins, so the baseline is given its full history first (120 entries,
    0.18 dB per bin); with a few entries the same reading scatters by up to 0.7 dB.
    """
    tracker = make_tracker()
    bench = Bench(tracker, seed=60)
    lin = floor_lin([PASSBAND])
    sig = lin * (1.0 + 10.0 ** (cn0_dbhz / 10.0) / BIN_HZ * tone_shape(position))
    bench.run(lin, 1300)
    measured = []
    for _ in range(200):
        t = bench.t_ns
        dets = bench.step(sig)
        if dets and dets[0].last_seen_ns == t:
            measured.append(dets[0].cn0_dbhz)
            lo, hi = bins_of(dets[0])
            assert lo - 1 <= position <= hi + 1 and hi - lo <= 4
    assert len(measured) > 150
    assert abs(float(np.median(measured)) - cn0_dbhz) < 1.0
    assert abs(float(np.mean(measured)) - cn0_dbhz) < 1.0


def test_cn0_needs_the_main_lobe_skirt_bins():
    """Why the C/N0 sum takes one bin on each side of the extent.

    A bin-centred tone at 24 dB-Hz has only its peak bin above threshold; the sum of
    MATH.md over that run alone holds 2/3 of the power and reads 1.76 dB low.
    """
    tracker = make_tracker()
    bench = Bench(tracker, seed=61)
    lin = floor_lin([PASSBAND])
    shape = tone_shape(50.0)
    np.testing.assert_allclose(shape[49:52], [1 / 6, 2 / 3, 1 / 6], atol=1e-9)
    sig = lin * (1.0 + 10.0 ** 2.4 / BIN_HZ * shape)
    assert 10.0 * np.log10(sig[50] / lin[50]) == pytest.approx(8.0, abs=0.2)
    assert 10.0 * np.log10(sig[49] / lin[49]) < 4.0
    bench.run(lin, 1300)
    reported, run_only = [], []
    for _ in range(300):
        t = bench.t_ns
        dets = bench.step(sig)
        if not dets or dets[0].last_seen_ns != t:
            continue
        reported.append(dets[0].cn0_dbhz)
        base = tracker.baseline_db.astype(np.float64)
        frame = bench.frames[-1].astype(np.float64)
        above = np.flatnonzero(frame[47:54] > base[47:54] + 6.0) + 47
        p, b = 10.0 ** (frame[above] / 10.0), 10.0 ** (base[above] / 10.0)
        run_only.append(10.0 * np.log10(np.sum(p - b) / np.mean(b) * BIN_HZ))
    assert abs(float(np.median(reported)) - 24.0) < 1.0
    assert float(np.median(reported)) - float(np.median(run_only)) > 0.8


@pytest.mark.parametrize("db_above", [8.0, 14.0])
def test_cn0_of_a_signal_spanning_40_bins(db_above):
    tracker = make_tracker()
    bench = Bench(tracker, seed=62)
    lin = floor_lin([PASSBAND])
    truth = 10.0 * np.log10(40 * (10.0 ** (db_above / 10.0) - 1.0) * BIN_HZ)  # C / N0 with N0 = floor / bin
    bench.run(lin, 150)
    history = bench.run(with_block(lin, 300, 40, db_above), 100)
    for dets in history[5:]:
        assert len(dets) == 1 and bins_of(dets[0]) == (300, 339)
        assert abs(dets[0].snr_db - db_above) < 1.5
    # one frame scatters by 0.3 dB (40 bins of chi-square(16)); the baseline is the noise
    # median, 0.18 dB under its mean at navg = 8, so the reading sits that much high
    errors = np.array([d[0].cn0_dbhz for d in history[5:]]) - truth
    assert abs(float(np.median(errors))) < 0.5
    assert np.max(np.abs(errors)) < 1.6 and np.mean(np.abs(errors) < 1.0) > 0.95


def _noise(rng: np.random.Generator, n: int) -> np.ndarray:
    """Unit-variance complex white noise."""
    return (rng.standard_normal(n) + 1j * rng.standard_normal(n)) / np.sqrt(2.0)


def test_cn0_and_frequency_of_an_iq_tone_through_welch_psd():
    """Real IQ: a tone of power C in unit-variance noise at fs has C/N0 = C * fs."""
    rng = np.random.default_rng(63)
    tracker = SignalTracker(CENTER_HZ, SPAN_HZ, NFFT, RATE_HZ, cfg(), [], [])
    offset_hz, cn0_dbhz = 4_321.0, 32.0
    amp = np.sqrt(10.0 ** (cn0_dbhz / 10.0) / SPAN_HZ)
    n = NFFT * NAVG
    measured = []
    for i in range(60):
        t = (i * n + np.arange(n)) / SPAN_HZ
        iq = _noise(rng, n) + (amp * np.exp(2j * np.pi * offset_hz * t) if i >= 20 else 0.0)
        dets = tracker.update(welch_psd_db(iq, SPAN_HZ, NFFT, NAVG), i * DT_NS)
        if i < 22:
            assert dets == []
        else:
            assert len(dets) == 1
            assert dets[0].f_lo_hz < CENTER_HZ + offset_hz < dets[0].f_hi_hz
            assert dets[0].f_hi_hz - dets[0].f_lo_hz <= 4.01 * BIN_HZ
            measured.append(dets[0].cn0_dbhz)
    assert abs(float(np.median(measured)) - cn0_dbhz) < 1.0


def test_cn0_of_iq_noise_spanning_40_bins_through_welch_psd():
    rng = np.random.default_rng(64)
    tracker = SignalTracker(CENTER_HZ, SPAN_HZ, NFFT, RATE_HZ, cfg(), [], [])
    n = NFFT * NAVG
    freq = np.fft.fftfreq(n, 1.0 / SPAN_HZ)
    band = np.abs(freq - 3_000.0) < 20 * BIN_HZ  # 40 bins wide, centred 3 kHz up
    power = 40 * (10.0 ** 0.9 - 1.0) / NFFT  # 9 dB above the floor in each of the 40 bins
    cn0_dbhz = 10.0 * np.log10(power * SPAN_HZ)
    measured = []
    for i in range(60):
        iq = _noise(rng, n)
        if i >= 20:
            shaped = np.fft.ifft(np.where(band, np.fft.fft(_noise(rng, n)), 0.0))
            iq = iq + shaped * np.sqrt(power / np.mean(np.abs(shaped) ** 2))
        dets = tracker.update(welch_psd_db(iq, SPAN_HZ, NFFT, NAVG), i * DT_NS)
        if i >= 25:
            assert len(dets) == 1
            assert abs((dets[0].f_lo_hz + dets[0].f_hi_hz) / 2 - (CENTER_HZ + 3_000.0)) < 2 * BIN_HZ
            assert abs((dets[0].f_hi_hz - dets[0].f_lo_hz) / BIN_HZ - 40) <= 3
            measured.append(dets[0].cn0_dbhz)
    assert abs(float(np.median(measured)) - cn0_dbhz) < 1.0


# ---------------------------------------------------------------- cost and validation


def test_update_costs_well_under_two_milliseconds_per_frame():
    tracker = make_tracker(carriers=[CARRIER])
    bench = Bench(tracker, seed=70)
    lin = floor_lin([PASSBAND])
    sig = with_block(with_block(carrier_lin(lin, 9.0), 200, 30, 10.0), 700, 3, 20.0)
    frames = [bench.rng.chisquare(2 * NAVG, NFFT) / (2 * NAVG) for _ in range(600)]
    frames = [(10.0 * np.log10((lin if i < 200 else sig) * f)).astype(np.float32) for i, f in enumerate(frames)]
    cost = []
    for i, psd in enumerate(frames):
        start = time.perf_counter()
        dets = tracker.update(psd, i * DT_NS)
        cost.append(time.perf_counter() - start)
    assert len(dets) == 3  # busy frames: three tracks, the carrier rule and history updates all ran
    # about 0.1 ms on an idle core; the bounds leave room for a loaded machine
    assert float(np.median(cost[200:])) < 1.0e-3, f"median {np.median(cost[200:]) * 1e6:.0f} us"
    assert float(np.mean(cost)) < 1.5e-3, f"mean {np.mean(cost) * 1e6:.0f} us"


def test_update_is_deterministic_and_does_not_keep_the_callers_array():
    lin = floor_lin([PASSBAND])
    sig = with_block(lin, 300, 12, 9.0)
    outputs = []
    for _ in range(2):
        tracker = make_tracker()
        rng = np.random.default_rng(71)
        out = []
        for i in range(200):
            psd = 10.0 * np.log10((lin if i < 150 else sig) * rng.chisquare(2 * NAVG, NFFT) / (2 * NAVG))
            out.append(tracker.update(psd, i * DT_NS))
            psd[:] = 99.0  # the caller reuses its buffer
        outputs.append(out)
    assert outputs[0] == outputs[1]
    assert outputs[0][-1] and outputs[0][-1][0].snr_db < 15.0


@pytest.mark.parametrize(
    "kwargs, name",
    [
        (dict(span_hz=0.0), "span_hz"),
        (dict(span_hz=float("nan")), "span_hz"),
        (dict(center_hz=float("inf")), "center_hz"),
        (dict(nfft=8), "nfft"),
        (dict(nfft=1024.5), "nfft"),
        (dict(frame_rate_hz=0.0), "frame_rate_hz"),
        (dict(passbands=[(2.0, 1.0, 0.0)]), "passbands[0]"),
        (dict(passbands=[(1.0, 2.0, -1.0)]), "passbands[0].relay_rise_db"),
        (dict(passbands=[(1.0, 2.0)]), "passbands[0]"),
        (dict(carriers=[("c", 2.0, 1.0)]), "carriers[0]"),
        (dict(carriers=[("c", 1.0)]), "carriers[0]"),
    ],
)
def test_constructor_rejects_bad_arguments(kwargs, name):
    args = dict(
        center_hz=CENTER_HZ, span_hz=SPAN_HZ, nfft=NFFT, frame_rate_hz=RATE_HZ, cfg=cfg(), passbands=[], carriers=[]
    )
    args.update(kwargs)
    with pytest.raises(ValueError, match=name.replace("[", r"\[").replace("]", r"\]")):
        SignalTracker(**args)


@pytest.mark.parametrize(
    "overrides, name",
    [
        (dict(threshold_db=0.0), "threshold_db"),
        (dict(threshold_db="6"), "threshold_db"),
        (dict(confirm_m=0), "confirm_m"),
        (dict(confirm_m=6), "confirm_m"),
        (dict(confirm_n=2.5), "confirm_n"),
        (dict(release_frames=0), "release_frames"),
        (dict(merge_bins=-1), "merge_bins"),
        (dict(baseline_tau_s=0.0), "baseline_tau_s"),
        (dict(baseline_tau_s=4.0), "baseline_tau_s"),  # 8 frame periods
        (dict(power_anomaly_db=-1.0), "power_anomaly_db"),
        (dict(power_anomaly_db=float("nan")), "power_anomaly_db"),
    ],
)
def test_constructor_rejects_bad_cfg(overrides, name):
    with pytest.raises(ValueError, match=f"cfg.{name}"):
        make_tracker(**overrides)


def test_constructor_names_a_missing_cfg_attribute():
    partial = cfg()
    del partial.release_frames
    with pytest.raises(ValueError, match="cfg.release_frames"):
        SignalTracker(CENTER_HZ, SPAN_HZ, NFFT, RATE_HZ, partial, [], [])


def test_update_rejects_bad_frames_and_times():
    tracker = make_tracker()
    good = np.zeros(NFFT, dtype=np.float32)
    with pytest.raises(ValueError, match="psd_db"):
        tracker.update(np.zeros(NFFT - 1), 0)
    bad = good.copy()
    bad[17] = np.nan
    with pytest.raises(ValueError, match="psd_db"):
        tracker.update(bad, 0)
    bad[17] = -np.inf
    with pytest.raises(ValueError, match="psd_db"):
        tracker.update(bad, 0)
    with pytest.raises(ValueError, match="t_ns"):
        tracker.update(good, 1.5e9)
    assert tracker.update(good, 10) == []
    with pytest.raises(ValueError, match="t_ns"):
        tracker.update(good, 10)
    with pytest.raises(ValueError, match="t_ns"):
        tracker.update(good, 9)
    assert tracker.update(good, np.int64(11)) == []


# ====================================================================== classification

FS = 50_000.0
N_IQ = 16_384


def _tone(rng: np.random.Generator, n: int, fs: float) -> tuple[np.ndarray, dict]:
    f = rng.uniform(-0.4, 0.4) * fs
    return np.exp(1j * (2.0 * np.pi * f * np.arange(n) / fs + rng.uniform(0.0, 2.0 * np.pi))), {"f": f}


def _chirp(rng: np.random.Generator, n: int, fs: float) -> tuple[np.ndarray, dict]:
    """Linear sawtooth FM, up or down, time-bandwidth product at least 150."""
    bw = rng.uniform(0.3, 0.8) * fs
    fc = rng.uniform(-1.0, 1.0) * (0.45 * fs - bw / 2.0)
    period = max(rng.uniform(200.0, 2000.0) / fs, 150.0 / bw)
    sign = float(rng.choice([-1.0, 1.0]))
    t = np.arange(n) / fs + rng.uniform(0.0, period)
    u = np.mod(t, period)
    x = np.exp(2j * np.pi * (fc * t + sign * (-0.5 * bw * u + 0.5 * bw / period * u * u)))
    return x, {"bw": bw, "fc": fc, "rate": sign * bw / period}


def _band_noise(rng: np.random.Generator, n: int, fs: float, bw: float | None = None) -> tuple[np.ndarray, dict]:
    """Complex Gaussian noise, flat over `bw` (default 0.1 .. 0.8 fs), unit power."""
    bw = rng.uniform(0.1, 0.8) * fs if bw is None else bw
    fc = rng.uniform(-1.0, 1.0) * (0.45 * fs - bw / 2.0)
    spec = np.fft.fft(_noise(rng, n))
    spec[np.abs(np.fft.fftfreq(n, 1.0 / fs) - fc) > bw / 2.0] = 0.0
    x = np.fft.ifft(spec)
    return x / np.sqrt(np.mean(np.abs(x) ** 2)), {"bw": bw, "fc": fc}


def _rrc(beta: float, sps: int, span: int = 10) -> np.ndarray:
    """Root-raised-cosine pulse, `span` symbols each side, `sps` samples per symbol."""
    t = np.arange(-span * sps, span * sps + 1) / sps
    with np.errstate(divide="ignore", invalid="ignore"):
        h = (np.sin(np.pi * t * (1 - beta)) + 4 * beta * t * np.cos(np.pi * t * (1 + beta))) / (
            np.pi * t * (1 - (4 * beta * t) ** 2)
        )
    h[t == 0] = 1 - beta + 4 * beta / np.pi
    singular = np.abs(np.abs(4 * beta * t) - 1) < 1e-9
    h[singular] = beta / np.sqrt(2) * (
        (1 + 2 / np.pi) * np.sin(np.pi / (4 * beta)) + (1 - 2 / np.pi) * np.cos(np.pi / (4 * beta))
    )
    return h


def _qpsk(rng: np.random.Generator, n: int, fs: float) -> tuple[np.ndarray, dict]:
    """QPSK, root-raised-cosine shaped, 2 .. 8 samples per symbol, roll-off 0.2 .. 0.5, unit power."""
    sps = int(rng.integers(2, 9))
    beta = rng.uniform(0.2, 0.5)
    nsym = n // sps + 40
    symbols = (rng.choice([-1.0, 1.0], nsym) + 1j * rng.choice([-1.0, 1.0], nsym)) / np.sqrt(2.0)
    up = np.zeros(nsym * sps, dtype=complex)
    up[::sps] = symbols
    x = np.convolve(up, _rrc(beta, sps), mode="same")[20 * sps : 20 * sps + n]
    fc = rng.uniform(-1.0, 1.0) * max(0.45 * fs - (1 + beta) * fs / sps / 2.0, 0.0)
    x = x * np.exp(2j * np.pi * fc * np.arange(n) / fs)
    return x / np.sqrt(np.mean(np.abs(x) ** 2)), {"sps": sps, "beta": beta, "fc": fc, "rate": fs / sps}


def _gated_noise(rng: np.random.Generator, n: int, fs: float) -> tuple[np.ndarray, dict]:
    """Band-limited noise switched on for `duty` of every period; unit power while on."""
    x, info = _band_noise(rng, n, fs, bw=rng.uniform(0.2, 0.8) * fs)
    period = int(rng.uniform(500, 4000))
    duty = rng.uniform(0.2, 0.7)
    gate = np.mod(np.arange(n) + rng.integers(0, period), period) < duty * period
    return x * gate, {**info, "duty": float(np.mean(gate)), "period": period}


GENERATORS = {"cw": _tone, "swept": _chirp, "noise": _band_noise, "modulated": _qpsk, "pulsed": _gated_noise}


def make_iq(label: str, seed: int, snr_db: float, n: int = N_IQ, fs: float = FS) -> tuple[np.ndarray, dict, np.ndarray]:
    """Signal of class `label` plus unit-variance white noise.

    snr_db is signal power over noise power in the whole capture band (for `pulsed`,
    the power while the gate is on). Returns (iq, generator parameters, clean signal).
    """
    rng = np.random.default_rng([seed, list(GENERATORS).index(label)])
    clean, info = GENERATORS[label](rng, n, fs)
    clean = np.sqrt(10.0 ** (snr_db / 10.0)) * clean
    return clean + _noise(rng, n), info, clean


def test_generators_have_the_stated_power():
    for label in GENERATORS:
        iq, info, clean = make_iq(label, 0, 10.0)
        on_fraction = info.get("duty", 1.0)
        assert np.mean(np.abs(clean) ** 2) == pytest.approx(10.0 * on_fraction, rel=0.05), label
        assert np.mean(np.abs(iq - clean) ** 2) == pytest.approx(1.0, rel=0.05)


@pytest.mark.parametrize("snr_db, need", [(10.0, 45), (20.0, 48)], ids=["10 dB: 90 %", "20 dB: 95 %"])
@pytest.mark.parametrize("label", list(GENERATORS))
def test_classification_accuracy_over_50_seeded_trials(label, snr_db, need):
    results = [classify(make_iq(label, seed, snr_db)[0], FS) for seed in range(50)]
    labels = [r.label for r in results]
    assert labels.count(label) >= need, {lab: labels.count(lab) for lab in set(labels)}
    assert all(r.label in CLASS_LABELS and 0.0 <= r.confidence <= 1.0 for r in results)
    right = [r.confidence for r in results if r.label == label]
    assert np.median(right) > 0.7  # confident when right at these SNRs


@pytest.mark.parametrize("n", [1024, 4096, 16_384, 20_000, 65_536])
def test_noise_only_input_is_unknown_with_low_confidence(n):
    for seed in range(40):
        result = classify(_noise(np.random.default_rng([seed, n]), n), FS)
        assert result.label == "unknown" and result.confidence <= 0.2, (n, seed, result)


def test_noise_filling_the_whole_capture_band_is_unknown():
    """No floor is visible, so white noise 10 dB up cannot be told from a noisier receiver."""
    rng = np.random.default_rng(80)
    result = classify(np.sqrt(11.0) * _noise(rng, N_IQ), FS)
    assert result.label == "unknown" and result.confidence == 0.0


def test_relayed_noise_step_of_a_transponder_edge_is_not_a_signal():
    """A 1.6 dB step in the floor across 60 % of the capture (a passband edge) stays unknown."""
    for seed in range(20):
        rng = np.random.default_rng([81, seed])
        pedestal, _ = _band_noise(rng, N_IQ, FS, bw=0.6 * FS)
        iq = _noise(rng, N_IQ) + np.sqrt((10.0 ** 0.16 - 1.0) * 0.6) * pedestal
        assert classify(iq, FS).label == "unknown"


def test_cw_features_match_the_tone():
    for seed in range(5):
        iq, info, _ = make_iq("cw", seed, 10.0)
        result = classify(iq, FS)
        f = result.features
        assert result.label == "cw" and result.confidence > 0.9
        assert abs(f["center_offset_hz"] - info["f"]) <= FS / N_IQ
        assert f["occupied_bw_hz"] <= 3 * FS / N_IQ
        assert f["line_share"] > 0.95
        assert f["envelope_kurtosis"] == pytest.approx(1.0, abs=0.05)
        # tone power over the noise in the five bins of its main lobe: SNR * n / 5
        assert f["snr_db"] == pytest.approx(10.0 + 10.0 * np.log10(N_IQ / 5), abs=1.0)


def test_weak_cw_below_the_capture_noise_is_still_cw():
    """25 dB-Hz in a 50 kHz capture is -22 dB SNR, but 20 dB above the noise in a 3 Hz bin."""
    for seed in range(10):
        rng = np.random.default_rng([82, seed])
        tone, info = _tone(rng, N_IQ, FS)
        result = classify(np.sqrt(10.0 ** 2.5 / FS) * tone + _noise(rng, N_IQ), FS)
        assert result.label == "cw"
        assert abs(result.features["center_offset_hz"] - info["f"]) <= FS / N_IQ
        # C/N0 over the noise bandwidth of a Hann bin (1.5 fs / n), less up to 1.4 dB of scalloping
        on_bin = 25.0 - 10 * np.log10(1.5 * FS / N_IQ)
        assert on_bin - 5.0 < result.features["line_snr_db"] < on_bin + 3.0


def test_swept_features_match_the_chirp():
    for seed in range(8):
        iq, info, _ = make_iq("swept", seed, 10.0)
        result = classify(iq, FS)
        f = result.features
        assert result.label == "swept"
        assert f["sweep_rate_hz_per_s"] == pytest.approx(info["rate"], rel=0.15)  # signed, Hz/s
        assert f["occupied_bw_hz"] == pytest.approx(info["bw"], rel=0.1)
        assert f["center_offset_hz"] == pytest.approx(info["fc"], abs=0.05 * info["bw"])
        assert f["if_linearity"] > 0.8 and f["tf_concentration"] > 3.0
        assert f["envelope_kurtosis"] == pytest.approx(1.0, abs=0.2)


def test_noise_features_match_the_band_and_the_snr():
    for seed in range(8):
        iq, info, _ = make_iq("noise", seed, 10.0)
        result = classify(iq, FS)
        f = result.features
        assert result.label == "noise"
        assert f["occupied_bw_hz"] == pytest.approx(info["bw"], rel=0.06)
        assert f["center_offset_hz"] == pytest.approx(info["fc"], abs=0.03 * info["bw"])
        assert f["envelope_kurtosis"] == pytest.approx(2.0, abs=0.15)  # Gaussian
        assert f["spectral_flatness"] > 0.6 and f["tf_concentration"] < 1.3
        assert f["duty_cycle"] == 1.0 and f["symbol_rate_hz"] == 0.0


def test_in_band_snr_feature():
    """Noise of bandwidth 0.4 fs at 10 dB capture SNR: the band the classifier keeps is
    the occupied band plus 15 % each side and 2 PSD bins, so SNR there is 10 / (0.52 + 5/1024)."""
    values = []
    for seed in range(10):
        rng = np.random.default_rng([83, seed])
        sig, _ = _band_noise(rng, N_IQ, FS, bw=0.4 * FS)
        values.append(classify(np.sqrt(10.0) * sig + _noise(rng, N_IQ), FS).features["snr_db"])
    assert np.mean(values) == pytest.approx(10.0 * np.log10(10.0 / (0.4 * 1.3 + 5 / 1024)), abs=0.5)


def test_modulated_features_match_the_qpsk():
    lines = 0
    for seed in range(12):
        iq, info, clean = make_iq("modulated", seed, 10.0)
        result = classify(iq, FS)
        f = result.features
        assert result.label == "modulated"
        # the kurtosis is that of the clean signal, although it was measured in noise
        true_kurtosis = np.mean(np.abs(clean) ** 4) / np.mean(np.abs(clean) ** 2) ** 2
        assert f["envelope_kurtosis"] == pytest.approx(true_kurtosis, abs=0.1)
        assert f["occupied_bw_hz"] == pytest.approx(info["rate"], rel=0.12)  # half-power width = symbol rate
        if info["sps"] >= 3:  # at 2 samples per symbol the line is at Nyquist
            assert f["symbol_rate_hz"] == pytest.approx(info["rate"], rel=0.005)
            assert f["symbol_line_db"] > 3.0
            lines += 1
    assert lines >= 6


def test_symbol_rate_above_half_the_sample_rate_is_unaliased():
    """1.6 samples per symbol (a capture at 1.25 x the occupied band): the line of |x|^2
    sits at fs - rate; the reported rate is the true one."""
    for seed in range(6):
        rng = np.random.default_rng([85, seed])
        nsym = 5 * N_IQ // 8 + 40
        up = np.zeros(nsym * 8, dtype=complex)
        up[::8] = (rng.choice([-1.0, 1.0], nsym) + 1j * rng.choice([-1.0, 1.0], nsym)) / np.sqrt(2.0)
        # 8 samples per symbol, band +-0.084 of that rate: every 5th sample is alias-free
        x = np.convolve(up, _rrc(0.35, 8), mode="same")[160 : 160 + 5 * N_IQ : 5]
        x = x / np.sqrt(np.mean(np.abs(x) ** 2))
        result = classify(np.sqrt(10.0) * x + _noise(rng, N_IQ), FS)
        assert result.label == "modulated"
        assert result.features["symbol_rate_hz"] == pytest.approx(FS / 1.6, rel=0.005)


def test_pulsed_features_match_the_gate():
    for seed in range(8):
        iq, info, _ = make_iq("pulsed", seed, 10.0)
        result = classify(iq, FS)
        f = result.features
        assert result.label == "pulsed"
        assert f["duty_cycle"] == pytest.approx(info["duty"], abs=0.04)
        # on level (signal + noise in the kept band) over off level (noise): at least the capture SNR
        assert f["envelope_contrast_db"] > 10.0
        assert f["envelope_kurtosis"] > 2.3


def test_other_waveforms_fall_in_the_sensible_class():
    """Not in the required set: gated tone, BPSK, 16-QAM, triangular sweep."""
    n, t = N_IQ, np.arange(N_IQ) / FS
    labels: dict[str, list[str]] = {"gated tone": [], "bpsk": [], "16qam": [], "triangle": []}
    for seed in range(10):
        rng = np.random.default_rng([84, seed])
        tone, _ = _tone(rng, n, FS)
        gate = np.mod(np.arange(n), 1500) < 600
        labels["gated tone"].append(classify(np.sqrt(10.0) * tone * gate + _noise(rng, n), FS).label)
        for name, alphabet in (
            ("bpsk", np.array([1.0, -1.0], dtype=complex)),
            ("16qam", np.array([a + 1j * b for a in (-3, -1, 1, 3) for b in (-3, -1, 1, 3)])),
        ):
            sps = int(rng.integers(3, 8))
            up = np.zeros((n // sps + 40) * sps, dtype=complex)
            up[::sps] = rng.choice(alphabet, n // sps + 40)
            x = np.convolve(up, _rrc(0.35, sps), mode="same")[20 * sps : 20 * sps + n]
            x = x / np.sqrt(np.mean(np.abs(x) ** 2))
            labels[name].append(classify(np.sqrt(10.0) * x + _noise(rng, n), FS).label)
        period = rng.uniform(500, 3000) / FS
        freq = 0.6 * FS * (np.abs(2.0 * np.mod(t, period) / period - 1.0) - 0.5)
        tri = np.exp(2j * np.pi * np.cumsum(freq) / FS)
        labels["triangle"].append(classify(np.sqrt(10.0) * tri + _noise(rng, n), FS).label)
    assert labels["gated tone"].count("pulsed") >= 9
    assert labels["bpsk"].count("modulated") >= 9
    assert labels["16qam"].count("modulated") >= 9
    assert labels["triangle"].count("swept") >= 9


def test_classification_does_not_depend_on_amplitude_scale_or_dtype():
    iq, _, _ = make_iq("modulated", 3, 10.0)
    reference = classify(iq, FS)
    for scaled in (1e-4 * iq, 3e3 * iq, iq.astype(np.complex64)):
        other = classify(scaled, FS)
        assert other.label == reference.label
        assert other.confidence == pytest.approx(reference.confidence, abs=1e-3)
        for key, value in reference.features.items():
            assert other.features[key] == pytest.approx(value, rel=1e-3, abs=1e-3), key


def test_frequencies_scale_with_fs():
    iq, _, _ = make_iq("modulated", 3, 10.0)
    slow, fast = classify(iq, FS), classify(iq, 10.0 * FS)
    assert fast.label == slow.label
    for key in ("occupied_bw_hz", "center_offset_hz", "symbol_rate_hz"):
        assert fast.features[key] == pytest.approx(10.0 * slow.features[key], rel=1e-9)
    assert fast.features["envelope_kurtosis"] == pytest.approx(slow.features["envelope_kurtosis"], rel=1e-3)
    assert fast.features["snr_db"] == pytest.approx(slow.features["snr_db"], abs=1e-2)


def test_classification_is_deterministic_json_safe_and_leaves_input_alone():
    for label in GENERATORS:
        iq, _, _ = make_iq(label, 1, 10.0)
        copy = iq.copy()
        first, second = classify(iq, FS), classify(iq, FS)
        assert isinstance(first, Classification) and first == second
        assert np.array_equal(iq, copy)
        assert all(isinstance(v, float) and np.isfinite(v) for v in first.features.values())
        json.dumps({"label": first.label, "confidence": first.confidence, "features": first.features}, allow_nan=False)
    silent = classify(np.zeros(4096, dtype=complex), FS)
    assert silent.label == "unknown" and silent.confidence == 0.0
    json.dumps(silent.features, allow_nan=False)


def test_long_capture_uses_the_first_262144_samples():
    iq, _, _ = make_iq("noise", 5, 10.0, n=262_144)
    longer = np.concatenate((iq, np.full(50_000, 1e6 + 0j)))  # garbage past the limit is not looked at
    assert classify(longer, FS) == classify(iq, FS)
    assert classify(iq, FS).label == "noise"


@pytest.mark.parametrize(
    "iq, fs, name",
    [
        (np.zeros((2, 4096), dtype=complex), FS, "iq"),
        (np.zeros(1000, dtype=complex), FS, "iq"),
        (np.array(["a"] * 2048), FS, "iq"),
        (np.full(4096, np.nan + 0j), FS, "iq"),
        (np.zeros(4096, dtype=complex), 0.0, "fs"),
        (np.zeros(4096, dtype=complex), float("nan"), "fs"),
        (np.zeros(4096, dtype=complex), "fast", "fs"),
    ],
)
def test_classify_rejects_bad_input(iq, fs, name):
    with pytest.raises(ValueError, match=name):
        classify(iq, fs)
