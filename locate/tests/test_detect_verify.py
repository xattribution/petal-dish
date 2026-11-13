"""Independent verification of core/detect.py (MATH.md section 7).

Written by the unit's verifier, not the implementer. Every frame and waveform is
made here: PSD frames are a known mean spectrum times an independent Gamma(navg) /
navg draw per bin (what `sim.world.render_psd` does), or real Welch estimates of
coloured Gaussian noise computed with this file's own FFT code. Expected values
come from the injected parameters or from closed forms (scipy.stats), never from
the tracker's own output.
"""
from __future__ import annotations

import collections
from fractions import Fraction
from types import SimpleNamespace

import numpy as np
import pytest
import scipy.signal
import scipy.stats

from petal_locate.core.detect import SignalTracker, classify

# ----------------------------------------------------------------------------- set-up

SPAN_HZ = 31_250.0
CENTER_HZ = 300.0e6
RATE_HZ = 2.0
DT_NS = 500_000_000
T0_NS = 1_790_000_000_123_456_789  # odd nanoseconds on purpose: time stays an exact int

DEFAULTS = dict(  # ARCHITECTURE.md section 3, processing.detection
    threshold_db=6.0,
    confirm_m=3,
    confirm_n=5,
    release_frames=6,
    merge_bins=2,
    baseline_tau_s=600.0,
    power_anomaly_db=6.0,
)
RISE_DB = 1.6  # relayed-noise rise of the transponder passband
PATIENCE = DEFAULTS["release_frames"] + DEFAULTS["confirm_n"]


def median_db(navg: int) -> float:
    """Median of a Gamma(navg)/navg noise bin relative to its mean, dB (closed form)."""
    return 10.0 * np.log10(scipy.stats.gamma.median(navg) / navg)


class Window:
    """A monitor window: thermal noise 1 plus relayed noise over `passfrac` of the span."""

    def __init__(self, nfft: int = 1024, passfrac: float = 0.8, span_hz: float = SPAN_HZ) -> None:
        self.nfft = nfft
        self.span_hz = span_hz
        self.bin_hz = span_hz / nfft
        self.freq = CENTER_HZ + (np.arange(nfft) - nfft // 2) * self.bin_hz
        self.floor = np.ones(nfft)
        self.passbands: list[tuple[float, float, float]] = []
        self.inpass = np.zeros(nfft, dtype=bool)
        if passfrac:
            lo, hi = CENTER_HZ - passfrac * span_hz / 2, CENTER_HZ + passfrac * span_hz / 2
            self.passbands = [(lo, hi, RISE_DB)]
            self.inpass = (self.freq >= lo) & (self.freq <= hi)
            self.floor[self.inpass] += 10.0 ** (RISE_DB / 10.0) - 1.0

    def tracker(self, carriers=(), **overrides: float) -> SignalTracker:
        cfg = SimpleNamespace(**{**DEFAULTS, **overrides})
        return SignalTracker(CENTER_HZ, self.span_hz, self.nfft, RATE_HZ, cfg, list(self.passbands), list(carriers))

    def edges(self, k0: int, k1: int) -> tuple[float, float]:
        return float(self.freq[k0] - self.bin_hz / 2), float(self.freq[k1] + self.bin_hz / 2)

    def signal(self, specs, base: np.ndarray | None = None) -> np.ndarray:
        """Mean spectrum with flat signals added: specs = [(first bin, width, S/N in dB over the local floor)]."""
        out = (self.floor if base is None else base).copy()
        for k0, width, snr_db in specs:
            out[k0 : k0 + width] += self.floor[k0 : k0 + width] * 10.0 ** (snr_db / 10.0)
        return out

    def bins(self, det) -> tuple[int, int]:
        lo = (det.f_lo_hz - CENTER_HZ) / self.bin_hz + self.nfft // 2 + 0.5
        hi = (det.f_hi_hz - CENTER_HZ) / self.bin_hz + self.nfft // 2 - 0.5
        assert abs(lo - round(lo)) < 1e-6 and abs(hi - round(hi)) < 1e-6
        return int(round(lo)), int(round(hi))


class Feed:
    """Feeds frames to a tracker: 'gamma' (render_psd model), 'welch' (real IQ, own FFT) or 'exact'."""

    def __init__(self, window: Window, tracker: SignalTracker, seed: int, navg: int = 8, mode: str = "gamma") -> None:
        self.window, self.tracker, self.navg, self.mode = window, tracker, navg, mode
        self.rng = np.random.default_rng(seed)
        self.i = 0
        n = window.nfft
        self.hann = 0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(n) / n)

    def frame(self, mean_lin: np.ndarray) -> np.ndarray:
        n = self.window.nfft
        if self.mode == "gamma":
            p = mean_lin * self.rng.gamma(self.navg, 1.0 / self.navg, n)
        elif self.mode == "welch":
            x = (self.rng.standard_normal((self.navg, n)) + 1j * self.rng.standard_normal((self.navg, n))) / np.sqrt(2.0)
            x = np.fft.ifft(np.fft.fft(x, axis=1) * np.sqrt(np.fft.ifftshift(mean_lin)), axis=1)
            spec = np.fft.fftshift(np.fft.fft(x * self.hann, axis=1), axes=1)
            p = np.mean(spec.real**2 + spec.imag**2, axis=0) / np.sum(self.hann**2)
        else:
            p = mean_lin
        return (10.0 * np.log10(p)).astype(np.float32)

    @property
    def t_ns(self) -> int:
        return T0_NS + self.i * DT_NS

    def step(self, mean_lin: np.ndarray):
        dets = self.tracker.update(self.frame(mean_lin), self.t_ns)
        self.i += 1
        return dets

    def run(self, mean_lin: np.ndarray, count: int):
        return [self.step(mean_lin) for _ in range(count)]


def assert_disjoint(window: Window, dets) -> None:
    spans = sorted(window.bins(d) for d in dets)
    for (_, hi), (lo, _) in zip(spans, spans[1:]):
        assert lo > hi, spans


# ------------------------------------------------------------ threshold vs frame statistics


@pytest.mark.parametrize("navg", [1, 2, 4, 8])
def test_threshold_in_force_is_the_gamma_exceedance_level_of_the_frames(navg):
    """10 log10(Q(1 - 1e-4) / Q(0.5)) of Gamma(navg) from scipy, or threshold_db when that is higher."""
    window = Window()
    feed = Feed(window, window.tracker(), seed=navg, navg=navg)
    feed.run(window.floor, 300)
    level = 10.0 * np.log10(scipy.stats.gamma.isf(1e-4, navg) / scipy.stats.gamma.median(navg))
    expected = max(DEFAULTS["threshold_db"], level)
    assert feed.tracker.effective_threshold_db == pytest.approx(expected, abs=0.1)
    if navg == 8:
        assert feed.tracker.effective_threshold_db == DEFAULTS["threshold_db"]


def test_threshold_is_not_raised_at_navg_8_by_a_crowded_window():
    """25 strong tones: their leakage skirts may lift it for the few frames before they are tracked, not longer."""
    window = Window()
    n = np.arange(window.nfft)
    hann = 0.5 - 0.5 * np.cos(2.0 * np.pi * n / window.nfft)
    lin = window.floor.copy()
    for pos in np.random.default_rng(1).uniform(110.0, 910.0, 25):
        tone = np.abs(np.fft.fftshift(np.fft.fft(hann * np.exp(2j * np.pi * (pos - window.nfft // 2) * n / window.nfft)))) ** 2
        lin += 1e6 * window.nfft * tone / tone.sum()
    feed = Feed(window, window.tracker(), seed=3)
    in_force = []
    for _ in range(300):
        feed.step(lin)
        in_force.append(feed.tracker.effective_threshold_db)
    assert all(thr == DEFAULTS["threshold_db"] for thr in in_force[10:])


# ------------------------------------------------------------------------ false tracks


def test_no_false_track_in_20000_noise_frames_at_navg_8():
    """1024 bins x 20000 frames x P(> 6 dB over the median) = 3.5e-7 gives ~7 single-bin crossings, none confirmable."""
    assert scipy.stats.gamma.sf(scipy.stats.gamma.median(8) * 10**0.6, 8) < 4e-7
    window = Window()
    feed = Feed(window, window.tracker(), seed=20000)
    listed = sum(1 for _ in range(20000) if feed.step(window.floor))
    assert listed == 0


def test_no_false_track_in_20000_welch_frames_of_a_64_bin_monitor():
    """A derived nfft = 64 monitor (13 guard bins), real Welch frames with correlated neighbouring bins."""
    window = Window(nfft=64, span_hz=1600.0)
    assert int(np.count_nonzero(~window.inpass)) == 13
    feed = Feed(window, window.tracker(), seed=64, mode="welch")
    listed = sum(1 for _ in range(20000) if feed.step(window.floor))
    assert listed == 0


@pytest.mark.parametrize("navg", [1, 2, 4])
def test_64_bin_monitor_with_few_averages_raises_its_threshold_and_stays_quiet(navg):
    window = Window(nfft=64, span_hz=1600.0)
    feed = Feed(window, window.tracker(), seed=100 + navg, navg=navg)
    listed = sum(1 for _ in range(4000) if feed.step(window.floor))
    assert listed == 0
    assert feed.tracker.effective_threshold_db > DEFAULTS["threshold_db"]


# --------------------------------------------------------------------------- start-up

STARTUP_CASES = {
    "10 bins in the passband": (0.8, [(500, 10, 10.0)]),
    "10 bins in the guard band": (0.8, [(40, 10, 10.0)]),
    "1 bin in the guard band": (0.8, [(20, 1, 12.0)]),
    "straddling the passband edge": (0.8, [(93, 20, 10.0)]),
    "over every lower guard bin": (0.8, [(0, 103, 10.0)]),
    "inside and outside at once": (0.8, [(500, 10, 10.0), (50, 8, 10.0)]),
    "no guard bins, 600 bins wide": (1.2, [(100, 600, 10.0)]),
}


@pytest.mark.parametrize("case", list(STARTUP_CASES))
def test_emitter_on_at_start_up_is_listed_from_frame_m_and_never_absorbed(case):
    passfrac, specs = STARTUP_CASES[case]
    window = Window(passfrac=passfrac)
    if case == "over every lower guard bin":  # more than half of all guard bins
        assert specs[0][1] == int(np.flatnonzero(window.inpass)[0]) and specs[0][1] > np.count_nonzero(~window.inpass) / 2
    feed = Feed(window, window.tracker(), seed=7)
    lin = window.signal(specs)
    hist = feed.run(lin, 1500)  # 750 s, longer than baseline_tau_s
    m = DEFAULTS["confirm_m"]
    assert all(h == [] for h in hist[: m - 1])
    want = sorted((k0, k0 + width - 1) for k0, width, _ in specs)
    for dets in hist[m - 1 :]:
        assert sorted(window.bins(d) for d in dets) == want
    assert len({d.track_id for h in hist for d in h}) == len(specs)
    # snr_db: the run's mean power over the mean (median-type) baseline, MATH section 7
    for k0, width, _ in specs:
        truth = 10.0 * np.log10(lin[k0 : k0 + width].mean() / window.floor[k0 : k0 + width].mean()) - median_db(8)
        snrs = [d.snr_db for h in hist[m - 1 :] for d in h if window.bins(d)[0] == k0]
        assert np.median(snrs) == pytest.approx(truth, abs=0.3)
        # frozen on the prior: the baseline under the emitter is still the noise median
        base = feed.tracker.baseline_db[k0 : k0 + width]
        assert np.mean(base - 10.0 * np.log10(window.floor[k0 : k0 + width])) == pytest.approx(median_db(8), abs=0.3)


@pytest.mark.parametrize(
    "specs", [[(30, 1, 10.0)], [(1, 3, 8.0)], [(7, 51, 8.0)]], ids=["tone in passband", "3 guard bins", "whole passband"]
)
def test_64_bin_monitor_detects_start_up_emitters(specs):
    window = Window(nfft=64, span_hz=1600.0)
    assert np.flatnonzero(window.inpass)[[0, -1]].tolist() == [7, 57]
    feed = Feed(window, window.tracker(), seed=5)
    hist = feed.run(window.signal(specs), 300)
    k0, width, _ = specs[0]
    for dets in hist[DEFAULTS["confirm_m"] - 1 :]:
        assert [window.bins(d) for d in dets] == [(k0, k0 + width - 1)]
    assert len({d.track_id for h in hist for d in h}) == 1


# ------------------------------------------------------------------------ slow changes


@pytest.mark.parametrize("seed", [0, 1])
def test_slowly_fading_signal_keeps_its_id_while_above_threshold_and_is_released(seed):
    """40 bins fading from 12 dB S/N to -10 dB over 600 s, then off."""
    window = Window()
    feed = Feed(window, window.tracker(), seed=seed)
    feed.run(window.floor, 200)
    level = 12.0 - 22.0 * np.minimum(np.arange(1300) / 1199.0, 1.0)
    strong = np.flatnonzero(level >= 7.0)  # whole signal 7.8 dB over its floor or more
    hist = []
    for i, lv in enumerate(level):
        hist.append(feed.step(window.signal([(400, 40, lv)]) if i < 1200 else window.floor))
        if i == strong[-1]:  # frozen all along: the baseline under it is still the noise median
            base = feed.tracker.baseline_db[400:440] - 10.0 * np.log10(window.floor[400:440])
            assert np.mean(base) == pytest.approx(median_db(8), abs=0.3)
    for i in strong[DEFAULTS["confirm_m"] - 1 :]:
        assert [(d.track_id, window.bins(d)) for d in hist[i]] == [(1, (400, 439))], i
    # nothing is listed once the signal is 3 dB under the noise (plus release) or gone
    gone = int(np.flatnonzero(level < -3.0)[0]) + DEFAULTS["release_frames"]
    assert all(h == [] for h in hist[gone:])
    for dets in hist:
        assert_disjoint(window, dets)


def test_signal_rising_by_12_db_in_five_minutes_ends_as_one_track_over_its_bins():
    window = Window()
    feed = Feed(window, window.tracker(), seed=4)
    feed.run(window.floor, 200)
    level = 12.0 * np.minimum(np.arange(800) / 599.0, 1.0)
    hist = [feed.step(window.signal([(400, 40, lv)])) for lv in level]
    for dets in hist:
        assert_disjoint(window, dets)
    first = next(i for i, h in enumerate(hist) if h)
    assert level[first] < 8.0
    assert len(hist[650]) == 1
    track_id = hist[650][0].track_id
    for dets in hist[650:]:
        assert [(d.track_id, window.bins(d)) for d in dets] == [(track_id, (400, 439))]


# ---------------------------------------------------------------------- merge and split


@pytest.mark.parametrize("gap", [3, 6, 12, 13, 20])
def test_two_signals_bridged_merge_under_the_lower_id_and_split_back_with_their_ids(gap):
    window = Window()
    feed = Feed(window, window.tracker(), seed=gap)
    feed.run(window.floor, 150)
    a, b = (300, 20, 12.0), (320 + gap, 20, 12.0)
    bridge = (320, gap, 12.0)
    apart = feed.run(window.signal([a, b]), 30)
    joined = feed.run(window.signal([a, b, bridge]), 20)
    split = feed.run(window.signal([a, b]), 40)
    two = [(1, (300, 319)), (2, (320 + gap, 339 + gap))]
    assert [(d.track_id, window.bins(d)) for d in apart[-1]] == two
    # merged after confirm_m consecutive shared frames, under the lower id, over the hull
    m = DEFAULTS["confirm_m"]
    for dets in joined[m:]:
        assert [(d.track_id, window.bins(d)) for d in dets] == [(1, (300, 339 + gap))]
    # split once the gap has been quiet for release_frames + confirm_n frames
    for dets in split[PATIENCE + 1 :]:
        assert [(d.track_id, window.bins(d)) for d in dets] == two
    for dets in apart + joined + split:
        assert_disjoint(window, dets)


def test_when_the_signal_a_merged_track_is_named_after_ends_the_other_keeps_its_own_id():
    window = Window()
    feed = Feed(window, window.tracker(), seed=5)
    feed.run(window.floor, 150)
    a, b, bridge = (300, 20, 12.0), (325, 20, 12.0), (320, 5, 12.0)
    before = feed.run(window.signal([a, b]), 20)[-1]
    assert [(d.track_id, window.bins(d)) for d in before] == [(1, (300, 319)), (2, (325, 344))]
    t_b = before[1].first_seen_ns
    merged = feed.run(window.signal([a, b, bridge]), 20)
    assert [d.track_id for d in merged[-1]] == [1]
    alone = feed.run(window.signal([b]), 40)
    for dets in alone[DEFAULTS["confirm_n"] + 1 :]:
        assert [(d.track_id, window.bins(d), d.first_seen_ns) for d in dets] == [(2, (325, 344), t_b)]


def test_fragment_beyond_a_second_track_does_not_stretch_the_first_over_it():
    """Noise-free: A = 300..319 and B = 330..339 confirmed; C = 345..347 appears; next frame one
    run 300..342 touches A, B and C while C has a run of its own (346..347)."""
    window = Window()
    feed = Feed(window, window.tracker(), seed=0, mode="exact")
    feed.run(window.floor, 150)
    two = window.signal([(300, 20, 12.0), (330, 10, 12.0)])
    assert [window.bins(d) for d in feed.run(two, 10)[-1]] == [(300, 319), (330, 339)]
    feed.step(window.signal([(345, 3, 12.0)], base=two))
    dets = feed.step(window.signal([(300, 43, 12.0), (346, 2, 12.0)]))
    assert [d.track_id for d in dets] == [1, 2]
    assert_disjoint(window, dets)
    assert window.bins(dets[0])[1] < 330 and window.bins(dets[1])[1] == 347


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_detections_listed_together_never_share_a_bin(seed):
    """A wide signal near threshold confirms fragments that grow towards each other."""
    window = Window()
    feed = Feed(window, window.tracker(), seed=seed)
    feed.run(window.floor, 200)
    for i in range(600):
        assert_disjoint(window, feed.step(window.signal([(400, 40, 12.0 * i / 599.0)])))


# ------------------------------------------------------------------------------ C/N0


@pytest.mark.parametrize("nfft, span_hz", [(1024, SPAN_HZ), (64, 1600.0)])
@pytest.mark.parametrize("cn0_dbhz", [30.0, 45.0])
def test_cn0_of_an_iq_tone_through_this_files_welch(nfft, span_hz, cn0_dbhz):
    """Tone of power A^2 in unit-variance white noise sampled at span_hz: C/N0 = A^2 span_hz.
    MATH's median baseline reads it 0.18 dB high at navg 8 (closed form, median_db)."""
    window = Window(nfft=nfft, passfrac=0.0, span_hz=span_hz)
    tracker = window.tracker()
    rng = np.random.default_rng([nfft, int(cn0_dbhz)])
    amp = np.sqrt(10.0 ** (cn0_dbhz / 10.0) / span_hz)
    f0 = rng.uniform(-0.3, 0.3) * span_hz
    hann = 0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(nfft) / nfft)
    seen = []
    for i in range(200):
        k = np.arange(8 * nfft) + i * 8 * nfft
        x = amp * np.exp(2j * np.pi * f0 * k / span_hz) + _wgn(rng, 8 * nfft)
        spec = np.fft.fftshift(np.fft.fft(x.reshape(8, nfft) * hann, axis=1), axes=1)
        psd = 10.0 * np.log10(np.mean(np.abs(spec) ** 2, axis=0) / np.sum(hann**2))
        dets = tracker.update(psd.astype(np.float32), T0_NS + i * DT_NS)
        if i >= 20:
            assert len(dets) == 1 and dets[0].f_lo_hz <= CENTER_HZ + f0 <= dets[0].f_hi_hz
            seen.append(dets[0].cn0_dbhz)
    assert np.median(seen) == pytest.approx(cn0_dbhz - median_db(8), abs=0.25)


# -------------------------------------------------------------------- authorised carrier


def carrier_run(window: Window, step_db: float, seed: int, navg: int = 8, mode: str = "gamma", width: int = 40, **kw):
    carrier = ("c1",) + window.edges(400, 400 + width - 1)
    feed = Feed(window, window.tracker(carriers=[carrier], **kw), seed=seed, navg=navg, mode=mode)
    nominal = window.floor.copy()
    nominal[400 : 400 + width] *= 10.0 ** (15.0 / 10.0)
    assert all(h == [] for h in feed.run(nominal, 300))
    stepped = nominal.copy()
    stepped[400 : 400 + width] *= 10.0 ** (step_db / 10.0)
    return feed.run(stepped, 100)


@pytest.mark.parametrize("anomaly_db", [6.0, 10.0])
def test_noise_free_carrier_step_of_exactly_power_anomaly_db_is_not_reported(anomaly_db):
    window = Window()
    assert all(h == [] for h in carrier_run(window, anomaly_db, 0, mode="exact", power_anomaly_db=anomaly_db))
    above = carrier_run(window, anomaly_db + 0.01, 0, mode="exact", power_anomaly_db=anomaly_db)
    for dets in above[DEFAULTS["confirm_n"] - 1 :]:  # once the last confirm_n frames all carry the step
        assert len(dets) == 1 and dets[0].authorized_carrier_id == "c1" and dets[0].anomaly


@pytest.mark.parametrize("navg", [4, 8])
@pytest.mark.parametrize("seed", [0, 1, 2])
def test_noisy_carrier_is_reported_above_power_anomaly_db_and_not_below(navg, seed):
    """Half a dB either side of 6 dB. At navg 4 the median baseline sits 0.37 dB under the mean:
    compared as it stands, a 5.5 dB step read 5.9 dB and was reported in about 60 % of frames."""
    window = Window()
    assert all(h == [] for h in carrier_run(window, 0.0, seed, navg=navg))
    assert all(h == [] for h in carrier_run(window, 5.5, seed, navg=navg))
    above = carrier_run(window, 6.5, seed, navg=navg)
    for dets in above[DEFAULTS["confirm_n"] :]:
        assert len(dets) == 1 and dets[0].authorized_carrier_id == "c1" and dets[0].anomaly


# -------------------------------------------------------------------------- tracker input


@pytest.mark.parametrize("bad", [["x"] * 1024, np.zeros(1024, dtype=complex), None], ids=["text", "complex", "None"])
def test_update_rejects_frames_that_are_not_real_numbers_naming_psd_db(bad):
    window = Window()
    with pytest.raises(ValueError, match="psd_db"):
        window.tracker().update(bad, T0_NS)


# ---------------------------------------------------------------------- classification

FS = 31_250.0  # a 25 kHz channel captured at oversample 1.25
N_IQ = 32_768  # about the 1 s capture of capture.duration_s


def _wgn(rng, n):
    return (rng.standard_normal(n) + 1j * rng.standard_normal(n)) / np.sqrt(2.0)


def _unit(x):
    return x / np.sqrt(np.mean(np.abs(x) ** 2))


def _shift(x, f_hz):
    return x * np.exp(2j * np.pi * f_hz / FS * np.arange(x.size))


def _rrc(beta: float, sps: int, span: int = 8) -> np.ndarray:
    t = np.arange(-span * sps, span * sps + 1) / sps
    h = np.empty_like(t)
    for i, ti in enumerate(t):
        if abs(ti) < 1e-12:
            h[i] = 1.0 + beta * (4.0 / np.pi - 1.0)
        elif abs(abs(4.0 * beta * ti) - 1.0) < 1e-9:
            h[i] = beta / np.sqrt(2.0) * ((1 + 2 / np.pi) * np.sin(np.pi / (4 * beta)) + (1 - 2 / np.pi) * np.cos(np.pi / (4 * beta)))
        else:
            h[i] = (np.sin(np.pi * ti * (1 - beta)) + 4 * beta * ti * np.cos(np.pi * ti * (1 + beta))) / (
                np.pi * ti * (1 - (4 * beta * ti) ** 2)
            )
    return h


def _cw(rng, n):
    f0, drift = rng.uniform(-0.4, 0.4) * FS, rng.uniform(-5.0, 5.0)  # Hz, Hz/s
    t = np.arange(n) / FS
    return np.exp(1j * (2 * np.pi * (f0 * t + 0.5 * drift * t * t) + rng.uniform(0, 2 * np.pi))), FS


def _swept(rng, n):
    """Sawtooth up, sawtooth down or triangle FM, by integrating the instantaneous frequency."""
    bw, period = rng.uniform(0.5, 0.8) * FS, rng.uniform(4e-3, 20e-3)
    fc = rng.uniform(-1, 1) * (0.45 * FS - bw / 2)
    u = np.mod(np.arange(n) / FS / period + rng.uniform(), 1.0)
    shape = [u - 0.5, 0.5 - u, np.abs(2 * u - 1) - 0.5][int(rng.integers(0, 3))]
    return np.exp(1j * 2 * np.pi * np.cumsum(fc + bw * shape) / FS), bw


def _noise(rng, n, bw=None):
    bw = rng.uniform(0.3, 0.8) * FS if bw is None else bw
    taps = scipy.signal.firwin(401, 0.5 * bw, window=("kaiser", 9.0), fs=FS)
    x = scipy.signal.lfilter(taps, 1.0, _wgn(rng, n + 512))[512:]
    return _unit(_shift(x, rng.uniform(-1, 1) * (0.45 * FS - bw / 2))), bw


def _psk(rng, n):
    order, beta, occupied = int(rng.choice([2, 4, 8])), rng.uniform(0.2, 0.5), rng.uniform(0.4, 0.8) * FS
    rate = occupied / (1 + beta)
    nsym = int(np.ceil(n * rate / FS)) + 40
    symbols = np.exp(1j * (2 * np.pi * rng.integers(0, order, nsym) / order + (np.pi / 4 if order == 4 else 0.0)))
    shaped = scipy.signal.upfirdn(_rrc(beta, 16), symbols, 16)
    ratio = Fraction(FS / (rate * 16)).limit_denominator(400)
    y = scipy.signal.resample_poly(shaped, ratio.numerator, ratio.denominator)[int(20 * FS / rate) :][:n]
    return _unit(_shift(y, rng.uniform(-1, 1) * max(0.45 * FS - occupied / 2, 0.0))), occupied


def _pulsed(rng, n):
    """Noise, PSK or a tone keyed on and off (duty 0.2 .. 0.6, period 5 .. 60 ms); unit power while on."""
    kind = int(rng.integers(0, 3))
    x, bw = [_noise, _psk, _cw][kind](rng, n)
    period = rng.uniform(5e-3, 60e-3) * FS
    gate = np.mod(np.arange(n) + rng.uniform(0, period), period) < rng.uniform(0.2, 0.6) * period
    return x * gate, bw


IQ_GENERATORS = {"cw": _cw, "swept": _swept, "noise": _noise, "modulated": _psk, "pulsed": _pulsed}


def make_capture(label: str, seed: int, snr_db: float, n: int = N_IQ) -> np.ndarray:
    """Signal at `snr_db` over the noise in its own occupied band (a tone: over the whole
    capture band; pulsed: while on), plus unit-variance white noise."""
    rng = np.random.default_rng([97531, seed, list(IQ_GENERATORS).index(label)])
    clean, bw = IQ_GENERATORS[label](rng, n)
    return clean * np.sqrt(10.0 ** (snr_db / 10.0) * bw / FS) + _wgn(rng, n)


def test_capture_generators_have_the_stated_in_band_snr():
    for label in IQ_GENERATORS:
        rng = np.random.default_rng([97531, 0, list(IQ_GENERATORS).index(label)])
        clean, bw = IQ_GENERATORS[label](rng, N_IQ)
        on = np.abs(clean) > 0
        assert np.mean(np.abs(clean[on]) ** 2) == pytest.approx(1.0, rel=0.05), label
        spec = np.abs(np.fft.fft(clean)) ** 2
        freqs = np.fft.fftfreq(N_IQ, 1.0 / FS)
        if label in ("swept", "noise", "modulated"):  # 99 % of the power inside the stated band
            centre = np.sum(freqs * spec) / np.sum(spec)
            assert np.sum(spec[np.abs(freqs - centre) <= 0.5 * bw * 1.02]) / np.sum(spec) > 0.97, label


@pytest.mark.parametrize("snr_db, need", [(8.0, 27), (10.0, 28), (20.0, 29)], ids=["8 dB", "10 dB", "20 dB"])
@pytest.mark.parametrize("label", list(IQ_GENERATORS))
def test_classification_accuracy_with_independent_generators(label, snr_db, need):
    results = [classify(make_capture(label, seed, snr_db), FS) for seed in range(30)]
    tally = collections.Counter(r.label for r in results)
    assert tally[label] >= need, dict(tally)


def _msk(rng, n):
    rate = rng.uniform(0.25, 0.5) * FS
    bits = rng.choice([-1.0, 1.0], int(n * rate / FS) + 10)
    k = np.minimum((np.arange(n) / FS * rate).astype(int), bits.size - 1)
    return np.exp(1j * 2 * np.pi * np.cumsum(bits[k] * rate / 4.0) / FS), 1.5 * rate


def _qam16(rng, n):
    beta, occupied = 0.35, rng.uniform(0.4, 0.6) * FS
    rate = occupied / (1 + beta)
    nsym = int(n * rate / FS) + 40
    symbols = (2 * rng.integers(0, 4, nsym) - 3) + 1j * (2 * rng.integers(0, 4, nsym) - 3)
    ratio = Fraction(FS / (rate * 8)).limit_denominator(400)
    y = scipy.signal.resample_poly(scipy.signal.upfirdn(_rrc(beta, 8), symbols, 8), ratio.numerator, ratio.denominator)
    return _unit(y[int(20 * FS / rate) :][:n]), occupied


@pytest.mark.parametrize("gen", [_msk, _qam16], ids=["MSK", "16-QAM"])
def test_other_modulations_are_modulated(gen):
    labels = []
    for seed in range(20):
        rng = np.random.default_rng([4242, seed])
        clean, bw = gen(rng, N_IQ)
        labels.append(classify(clean * np.sqrt(10.0 * bw / FS) + _wgn(rng, N_IQ), FS).label)
    assert labels.count("modulated") >= 19, collections.Counter(labels)


@pytest.mark.parametrize("label", list(IQ_GENERATORS))
def test_short_4096_sample_captures_at_10_db(label):
    labels = [classify(make_capture(label, 500 + seed, 10.0, n=4096), FS).label for seed in range(20)]
    assert labels.count(label) >= 19, collections.Counter(labels)


def test_white_noise_alone_is_unknown():
    for seed in range(20):
        result = classify(_wgn(np.random.default_rng([31, seed]), N_IQ), FS)
        assert result.label == "unknown" and result.confidence == 0.0
