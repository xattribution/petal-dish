"""Spectrum frames to detections, one tracker per monitor window (ARCHITECTURE.md section 7).

For every (channel, monitor) of the configuration there is one
``core.detect.SignalTracker``, built as the detection engineer specified:

- passbands: every transponder of a satellite the channel watches whose downlink
  passband overlaps the window, in downlink Hz, with ``relay_rise_db`` of
  ``core.linkbudget`` for that channel and satellite (0 dB when the satellite is below
  the site's horizon at build time: its relayed noise does not reach the channel);
- carriers: the authorised carriers of the frequency plan mapped to downlink Hz
  through every watched satellite whose transponder relays the carrier's centre (an
  authorised uplink appears on every satellite that hears it, not only the one it is
  assigned to, and must not raise an event there either);
- ``frame_rate_hz`` from the monitor, the detection settings from
  ``processing.detection``.

``build_window_specs`` computes these (about 1 ms per channel and transponder, so it
runs in a worker thread); ``DetectionPipeline.install`` swaps them in and rebuilds a
tracker only when its inputs changed (a rebuilt tracker relearns its baseline). For
``confirm_n`` frames after a rebuild the detections of the old tracker are carried
along, so that an event does not go ``lost`` while the new tracker confirms.

``DetectionPipeline.on_psd`` is the hub's spectrum subscriber. Frames are dropped and
counted (``counters()``) when the window is unknown, ``nfft`` differs, the node
retuned by more than half a bin, a bin is not finite, or ``t_ns`` repeats or goes
backwards. Each accepted frame gives:

- a ``WindowReport`` to the sink (the correlator): the window, the frame times and
  every confirmed detection;
- when a console client streams that window, the binary spectrum frame of
  PROTOCOL.md section B (PSD, the baseline once the tracker has one, detections with
  their authorised-carrier flag and anomaly), offered to the console bus, which keeps
  the newest per window and client.

Cost: a tracker update is about 1 ms for 1024 bins. The cost is measured per window
(exponential average); a window whose updates average more than ``offload_cost_s``
(or with ``nfft >= OFFLOAD_NFFT``) is updated in the server's worker pool instead of
on the event loop.
"""
from __future__ import annotations

import logging
import math
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

import numpy as np

from petal_locate.core.detect import Detection, SignalTracker
from petal_locate.core.linkbudget import relay_rise_db
from petal_locate.protocol.framing import encode_console_psd

__all__ = [
    "COST_EWMA_ALPHA",
    "OFFLOAD_COST_S",
    "OFFLOAD_NFFT",
    "ConsolePsdSink",
    "DetectionPipeline",
    "DetectionSink",
    "WindowKey",
    "WindowReport",
    "WindowSpec",
    "build_trackers",
    "build_window_specs",
]

logger = logging.getLogger(__name__)

OFFLOAD_COST_S: Final[float] = 0.003
"""Average tracker update time above which a window is updated in a worker thread, s."""

OFFLOAD_NFFT: Final[int] = 16384
"""Windows with at least this many bins are always updated in a worker thread."""

COST_EWMA_ALPHA: Final[float] = 0.2
"""Weight of the newest update in the per-window average cost."""

WindowKey = tuple[str, str]
"""(channel_id, monitor_id)."""

_RISE_ROUND_DB: Final[float] = 0.5  # rises closer than this do not rebuild a tracker


@dataclass(frozen=True)
class WindowSpec:
    """One monitor window and what its tracker is built from.

    center_hz, span_hz, nfft, navg, frame_rate_hz: the monitor (downlink Hz).
    passbands: (f_lo_hz, f_hi_hz, relay_rise_db) of the transponder downlinks it sees.
    carriers: (carrier_id, f_lo_hz, f_hi_hz) of the authorised carriers, downlink Hz.
    watched: satellites the channel watches. noise_temp_k: the channel's system
    noise temperature, K. t_s: when the relay rises were evaluated, Unix s.
    """

    channel_id: str
    monitor_id: str
    site_id: str
    center_hz: float
    span_hz: float
    nfft: int
    navg: int
    frame_rate_hz: float
    passbands: tuple[tuple[float, float, float], ...]
    carriers: tuple[tuple[str, float, float], ...]
    watched: tuple[str, ...]
    noise_temp_k: float
    t_s: float = 0.0

    @property
    def key(self) -> WindowKey:
        """(channel_id, monitor_id)."""
        return (self.channel_id, self.monitor_id)

    @property
    def lo_hz(self) -> float:
        """Lower edge of the window, Hz."""
        return self.center_hz - 0.5 * self.span_hz

    @property
    def hi_hz(self) -> float:
        """Upper edge of the window, Hz."""
        return self.center_hz + 0.5 * self.span_hz

    @property
    def bin_hz(self) -> float:
        """Width of one bin, Hz."""
        return self.span_hz / self.nfft

    @property
    def frame_period_s(self) -> float:
        """Nominal time between frames, s."""
        return 1.0 / self.frame_rate_hz

    def contains(self, f_hz: float) -> bool:
        """Whether a frequency (Hz) lies inside the window."""
        return self.lo_hz <= f_hz <= self.hi_hz

    def tracker_inputs(self) -> tuple[Any, ...]:
        """What the tracker is built from (rises rounded): equal inputs keep a tracker."""
        return (
            round(self.center_hz, 3),
            round(self.span_hz, 3),
            self.nfft,
            round(self.frame_rate_hz, 9),
            tuple(
                (round(lo, 3), round(hi, 3), round(rise / _RISE_ROUND_DB) * _RISE_ROUND_DB)
                for lo, hi, rise in self.passbands
            ),
            tuple((cid, round(lo, 3), round(hi, 3)) for cid, lo, hi in self.carriers),
        )


@dataclass(frozen=True)
class WindowReport:
    """What the sink receives after each accepted frame of one window.

    generation: of the window's tracker (increases when it is rebuilt).
    t_ns: labelled time of the frame's first sample, integer UTC ns.
    received_ns: server clock when the frame arrived, integer UTC ns.
    detections: (tracker generation, detection) for every confirmed, unreleased
        detection, including those carried from a replaced tracker.
    threshold_db: the threshold the tracker tested this frame with, dB.
    """

    spec: WindowSpec
    generation: int
    t_ns: int
    received_ns: int
    detections: tuple[tuple[int, Detection], ...]
    threshold_db: float


class DetectionSink(Protocol):
    """Receives the detections of every window (the correlator)."""

    def update(self, report: WindowReport) -> None:
        """One accepted frame of one window."""

    def drop_window(self, key: WindowKey) -> None:
        """A window left the configuration: forget its detections."""


class ConsolePsdSink(Protocol):
    """Where console spectrum frames go (the runtime's console bus)."""

    def wants_psd(self, channel_id: str, monitor_id: str) -> bool:
        """Whether any console client streams this window."""

    def publish_psd(self, channel_id: str, monitor_id: str, frame: bytes) -> None:
        """Offer one binary frame (newest wins per window and client)."""


class WorkRunner(Protocol):
    """Runs a blocking function in a worker thread (the runtime's ``WorkPool``)."""

    async def run(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        """Run ``fn(*args, **kwargs)`` off the event loop and return its result."""


# --------------------------------------------------------------------- building


def build_window_specs(config: Any, t_s: float) -> dict[WindowKey, WindowSpec]:
    """The window specs of a configuration (blocking: run it in a worker thread).

    config: ``SystemConfig``. t_s: Unix seconds at which relay rises are evaluated.
    A relay rise that cannot be computed is logged and taken as 0 dB.
    """
    specs: dict[WindowKey, WindowSpec] = {}
    rises: dict[tuple[str, str, str], float] = {}

    def rise(channel_id: str, sat_id: str, transponder_id: str) -> float:
        key = (channel_id, sat_id, transponder_id)
        if key not in rises:
            try:
                value = relay_rise_db(config, channel_id, sat_id, transponder_id, t_s)
            except ValueError as exc:
                logger.warning(
                    "relay rise of channel %s on %s/%s could not be computed (%s); using 0 dB",
                    channel_id,
                    sat_id,
                    transponder_id,
                    exc,
                    extra={"channel_id": channel_id, "sat_id": sat_id},
                )
                value = None
            rises[key] = 0.0 if value is None or not math.isfinite(value) else max(float(value), 0.0)
        return rises[key]

    for site in config.sites:
        for channel in site.channels:
            watched = tuple(channel.watched_satellite_ids)
            for monitor in channel.monitors:
                lo = monitor.center_hz - 0.5 * monitor.span_hz
                hi = monitor.center_hz + 0.5 * monitor.span_hz
                passbands: list[tuple[float, float, float]] = []
                carriers: list[tuple[str, float, float]] = []
                for sat_id in watched:
                    satellite = config.satellite(sat_id)
                    for transponder in satellite.transponders:
                        if transponder.downlink_hi_hz <= lo or transponder.downlink_lo_hz >= hi:
                            continue
                        passbands.append(
                            (
                                float(transponder.downlink_lo_hz),
                                float(transponder.downlink_hi_hz),
                                rise(channel.id, sat_id, transponder.id),
                            )
                        )
                for carrier in config.carriers:
                    for sat_id in watched:
                        satellite = config.satellite(sat_id)
                        for transponder in satellite.transponders:
                            if not transponder.relays(carrier.uplink_center_hz):
                                continue
                            d_lo = carrier.uplink_lo_hz + transponder.translation_hz
                            d_hi = carrier.uplink_hi_hz + transponder.translation_hz
                            entry = (carrier.id, float(d_lo), float(d_hi))
                            if d_hi > lo and d_lo < hi and entry not in carriers:
                                carriers.append(entry)
                spec = WindowSpec(
                    channel_id=channel.id,
                    monitor_id=monitor.id,
                    site_id=site.id,
                    center_hz=float(monitor.center_hz),
                    span_hz=float(monitor.span_hz),
                    nfft=int(monitor.nfft),
                    navg=int(monitor.navg),
                    frame_rate_hz=float(monitor.frame_rate_hz),
                    passbands=tuple(passbands),
                    carriers=tuple(carriers),
                    watched=watched,
                    noise_temp_k=float(channel.system_noise_temp_k),
                    t_s=float(t_s),
                )
                specs[spec.key] = spec
    return specs


def _detection_inputs(detection_cfg: Any) -> tuple[Any, ...]:
    names = (
        "threshold_db",
        "confirm_m",
        "confirm_n",
        "release_frames",
        "merge_bins",
        "baseline_tau_s",
        "power_anomaly_db",
    )
    return tuple(getattr(detection_cfg, name) for name in names)


def build_trackers(
    detection_cfg: Any, specs: Iterable[WindowSpec]
) -> dict[WindowKey, SignalTracker | ValueError]:
    """New trackers for the given windows (blocking: may run in a worker thread).

    Returns a tracker per window, or the ``ValueError`` that made its construction
    fail (for example ``nfft`` below 16 or a baseline shorter than 10 frames).
    """
    out: dict[WindowKey, SignalTracker | ValueError] = {}
    for spec in specs:
        try:
            out[spec.key] = SignalTracker(
                spec.center_hz,
                spec.span_hz,
                spec.nfft,
                spec.frame_rate_hz,
                detection_cfg,
                passbands=list(spec.passbands),
                carriers=list(spec.carriers),
            )
        except ValueError as exc:
            out[spec.key] = exc
    return out


# --------------------------------------------------------------------- pipeline


@dataclass(eq=False)
class _Window:
    spec: WindowSpec
    tracker: SignalTracker | None
    inputs: tuple[Any, ...]
    generation: int
    last_t_ns: int | None = None
    last_received_ns: int | None = None
    cost_s: float | None = None
    offload: bool = False
    frames: int = 0
    carry: tuple[int, list[Detection], int] | None = None
    last_detections: list[Detection] = field(default_factory=list)
    retune_warned: bool = False
    updated: bool = False


class DetectionPipeline:
    """One tracker per monitor window; the hub's spectrum subscriber.

    Call ``install`` with the specs of the current configuration (again after every
    change), subscribe ``on_psd`` to the hub, and read ``counters()`` for what was
    dropped and why.
    """

    def __init__(
        self,
        *,
        sink: DetectionSink,
        console: ConsolePsdSink | None = None,
        pool: WorkRunner | None = None,
        offload_cost_s: float = OFFLOAD_COST_S,
    ) -> None:
        """
        sink: receives a ``WindowReport`` per accepted frame and window removals.
        console: console bus for spectrum frames (None: none are built).
        pool: worker pool for windows too costly for the event loop (None: always
            inline).
        offload_cost_s: average update time beyond which a window is offloaded, s.
        """
        self._sink = sink
        self._console = console
        self._pool = pool
        self._offload_cost_s = float(offload_cost_s)
        self._windows: dict[WindowKey, _Window] = {}
        self._confirm_n = 5
        self._counters: Counter[str] = Counter()
        self._costs: list[float] = []

    # ---------------------------------------------------------------- queries

    @property
    def windows(self) -> Mapping[WindowKey, WindowSpec]:
        """The installed window specs."""
        return {key: w.spec for key, w in self._windows.items()}

    def tracker(self, key: WindowKey) -> SignalTracker | None:
        """The tracker of a window (None when unknown or not buildable)."""
        window = self._windows.get(key)
        return None if window is None else window.tracker

    def generation(self, key: WindowKey) -> int | None:
        """Generation of a window's tracker."""
        window = self._windows.get(key)
        return None if window is None else window.generation

    def offloaded(self, key: WindowKey) -> bool:
        """Whether a window is updated in a worker thread."""
        window = self._windows.get(key)
        return bool(window and window.offload)

    def counters(self) -> dict[str, int]:
        """Frames accepted and dropped, by reason (``unknown_window``, ``no_tracker``,
        ``nfft_mismatch``, ``retuned``, ``non_finite``, ``repeated``,
        ``out_of_order``, ``stale_tracker``, ``tracker_error``, ``pool_closed``,
        ``sink_errors``, ``console_frames``, ``console_errors``)."""
        return dict(self._counters)

    def cost_stats(self) -> dict[str, float]:
        """Tracker update times of the most recent frames: count, median and maximum, s."""
        if not self._costs:
            return {"count": 0.0, "median_s": 0.0, "max_s": 0.0}
        values = sorted(self._costs)
        return {
            "count": float(len(values)),
            "median_s": values[len(values) // 2],
            "max_s": values[-1],
        }

    def needs_tracker(self, detection_cfg: Any, specs: Mapping[WindowKey, WindowSpec]) -> list[WindowSpec]:
        """The specs whose tracker would be (re)built by ``install``."""
        det = _detection_inputs(detection_cfg)
        out: list[WindowSpec] = []
        for key, spec in specs.items():
            window = self._windows.get(key)
            if window is None or window.inputs != (spec.tracker_inputs(), det):
                out.append(spec)
        return out

    # ---------------------------------------------------------------- install

    def install(
        self,
        detection_cfg: Any,
        specs: Mapping[WindowKey, WindowSpec],
        prebuilt: Mapping[WindowKey, SignalTracker | ValueError] | None = None,
    ) -> None:
        """Make these the windows of the pipeline (call on the event loop).

        detection_cfg: ``processing.detection`` of the configuration.
        specs: from ``build_window_specs``.
        prebuilt: trackers from ``build_trackers`` for the windows ``needs_tracker``
            named (any missing one is built here).
        Unchanged windows keep their tracker. Removed windows are reported to the sink.
        A tracker that cannot be built is logged; its window drops frames until the
        configuration is fixed.
        """
        det = _detection_inputs(detection_cfg)
        self._confirm_n = int(getattr(detection_cfg, "confirm_n", 5))
        built = dict(prebuilt or {})
        new_windows: dict[WindowKey, _Window] = {}
        rebuilt = 0
        for key, spec in specs.items():
            inputs = (spec.tracker_inputs(), det)
            old = self._windows.get(key)
            if old is not None and old.inputs == inputs:
                old.spec = spec
                new_windows[key] = old
                continue
            tracker = built.get(key)
            if tracker is None:
                tracker = build_trackers(detection_cfg, [spec])[key]
            if isinstance(tracker, ValueError):
                logger.error(
                    "no detection on window %s/%s: its tracker cannot be built (%s)",
                    spec.channel_id,
                    spec.monitor_id,
                    tracker,
                    extra={"channel_id": spec.channel_id, "monitor_id": spec.monitor_id},
                )
                tracker = None
            window = _Window(
                spec=spec,
                tracker=tracker,
                inputs=inputs,
                generation=(old.generation + 1) if old is not None else 1,
            )
            if old is not None:
                rebuilt += 1
                if old.last_detections:
                    window.carry = (old.generation, list(old.last_detections), self._confirm_n)
                window.last_t_ns = old.last_t_ns
            new_windows[key] = window
        removed = [key for key in self._windows if key not in new_windows]
        self._windows = new_windows
        for key in removed:
            try:
                self._sink.drop_window(key)
            except Exception:  # the sink must not break reconfiguration
                logger.exception("detection sink failed to drop window %s/%s", *key)
        logger.info(
            "detection windows installed: %d window(s), %d rebuilt, %d removed",
            len(new_windows),
            rebuilt,
            len(removed),
            extra={"windows": len(new_windows), "rebuilt": rebuilt, "removed": len(removed)},
        )

    # ----------------------------------------------------------------- frames

    def _drop(self, reason: str) -> None:
        self._counters[reason] += 1

    async def on_psd(self, frame: Any) -> None:
        """Process one spectrum frame (``hub.PsdFrame``). Never raises."""
        key = (frame.channel_id, frame.monitor_id)
        window = self._windows.get(key)
        if window is None:
            self._drop("unknown_window")
            return
        if window.tracker is None:
            self._drop("no_tracker")
            return
        header = frame.header
        spec = window.spec
        if header.nfft != spec.nfft:
            self._drop("nfft_mismatch")
            return
        if abs(header.center_hz - spec.center_hz) > 0.5 * spec.bin_hz or abs(header.span_hz - spec.span_hz) > 1e-6 * spec.span_hz:
            self._drop("retuned")
            if not window.retune_warned:
                window.retune_warned = True
                logger.warning(
                    "window %s/%s: frames arrive at %.1f Hz span %.1f Hz instead of %.1f / %.1f Hz; dropped",
                    spec.channel_id,
                    spec.monitor_id,
                    header.center_hz,
                    header.span_hz,
                    spec.center_hz,
                    spec.span_hz,
                    extra={"channel_id": spec.channel_id, "monitor_id": spec.monitor_id},
                )
            return
        psd = np.asarray(frame.psd_db)
        if psd.shape != (spec.nfft,) or not np.isfinite(psd).all():
            self._drop("non_finite")
            return
        t_ns = int(header.t_ns)
        if window.last_t_ns is not None and t_ns <= window.last_t_ns:
            self._drop("repeated" if t_ns == window.last_t_ns else "out_of_order")
            return
        window.last_t_ns = t_ns
        tracker = window.tracker
        started = time.perf_counter()
        try:
            if window.offload and self._pool is not None:
                detections = await self._pool.run(tracker.update, psd, t_ns)
            else:
                detections = tracker.update(psd, t_ns)
        except RuntimeError as exc:  # the worker pool closed: the server is stopping
            self._drop("pool_closed")
            logger.debug("window %s/%s: frame dropped (%s)", spec.channel_id, spec.monitor_id, exc)
            return
        except ValueError as exc:
            self._drop("tracker_error")
            logger.warning(
                "window %s/%s: frame rejected by the tracker (%s)",
                spec.channel_id,
                spec.monitor_id,
                exc,
                extra={"channel_id": spec.channel_id, "monitor_id": spec.monitor_id},
            )
            return
        cost = time.perf_counter() - started
        if self._windows.get(key) is not window:
            self._drop("stale_tracker")  # reconfigured while the update ran in a thread
            return
        self._note_cost(window, cost)
        window.frames += 1
        window.updated = True
        window.last_received_ns = int(frame.received_ns)
        current = list(detections)
        window.last_detections = current
        reported = [(window.generation, d) for d in current]
        if window.carry is not None:
            generation, carried, left = window.carry
            reported.extend((generation, d) for d in carried)
            window.carry = (generation, carried, left - 1) if left > 1 else None
        report = WindowReport(
            spec=spec,
            generation=window.generation,
            t_ns=t_ns,
            received_ns=int(frame.received_ns),
            detections=tuple(reported),
            threshold_db=float(tracker.effective_threshold_db),
        )
        self._counters["accepted"] += 1
        try:
            self._sink.update(report)
        except Exception:  # one bad report must not stop the spectrum pipeline
            self._counters["sink_errors"] += 1
            logger.exception(
                "detection sink failed on window %s/%s", spec.channel_id, spec.monitor_id
            )
        self._console_frame(window, header, psd, current)

    def _note_cost(self, window: _Window, cost: float) -> None:
        self._costs.append(cost)
        if len(self._costs) > 512:
            del self._costs[:256]
        window.cost_s = cost if window.cost_s is None else (1 - COST_EWMA_ALPHA) * window.cost_s + COST_EWMA_ALPHA * cost
        if not window.offload and self._pool is not None:
            if window.spec.nfft >= OFFLOAD_NFFT or window.cost_s > self._offload_cost_s:
                window.offload = True
                logger.info(
                    "window %s/%s: tracker updates take %.1f ms on average; moved to the worker pool",
                    window.spec.channel_id,
                    window.spec.monitor_id,
                    window.cost_s * 1e3,
                    extra={"channel_id": window.spec.channel_id, "monitor_id": window.spec.monitor_id},
                )

    def _console_frame(self, window: _Window, header: Any, psd: np.ndarray, detections: Sequence[Detection]) -> None:
        console = self._console
        spec = window.spec
        if console is None or not console.wants_psd(spec.channel_id, spec.monitor_id):
            return
        assert window.tracker is not None
        baseline: np.ndarray | None = window.tracker.baseline_db
        if baseline is not None and not np.isfinite(baseline).all():
            baseline = None
        try:
            frame = encode_console_psd(
                spec.channel_id,
                spec.monitor_id,
                int(header.t_ns),
                float(header.center_hz),
                float(header.span_hz),
                int(header.nfft),
                int(header.seq),
                np.ascontiguousarray(psd, dtype=np.float32),
                baseline,
                detections,
            )
        except ValueError as exc:
            self._counters["console_errors"] += 1
            logger.warning(
                "console spectrum frame of %s/%s could not be built (%s)",
                spec.channel_id,
                spec.monitor_id,
                exc,
                extra={"channel_id": spec.channel_id, "monitor_id": spec.monitor_id},
            )
            return
        self._counters["console_frames"] += 1
        console.publish_psd(spec.channel_id, spec.monitor_id, frame)
