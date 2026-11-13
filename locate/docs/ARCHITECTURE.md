# PETAL Locate — architecture and module contracts

PETAL Locate detects interference on bent-pipe satellite transponders, decides whether
it is on the uplink or local to one ground site, and geolocates uplink emitters from
time and frequency differences between copies relayed by two or more satellites.
Nothing is specific to one satellite, band or site: satellites, transponders, sites,
antennas, frequency plans and processing settings are all configuration.

Read `MATH.md` first. It fixes every sign, unit and algorithmic requirement.
Revision 2 (after independent review).

## 1. Layout

```
locate/
  pyproject.toml            Python package (venv at locate/.venv)
  config/                   example system configs and simulation scenarios (YAML)
  docs/                     MATH.md ARCHITECTURE.md PROTOCOL.md UI-BRIEF.md CONTRIBUTING.md
                            (later: API notes, RESULTS.md, LIMITATIONS.md, OPERATIONS.md)
  src/petal_locate/
    core/                   pure numerics: no I/O, no asyncio, no globals, never imports config/
      constants.py types.py (PathSpec, CalRef, Measurement, Uncertainty; re-exported by geometry.py and solver.py)
      timebase.py geodesy.py orbits.py geometry.py
      antennas.py linkbudget.py iono.py crlb.py spectrum.py
      signals.py channel.py
      caf.py detect.py
      solver.py lop.py accuracy.py
    config/                 models.py (pydantic), loader.py
    sim/                    scenario.py, world.py (truth + IQ/PSD synthesis), montecarlo.py
    protocol/               messages.py (pydantic), framing.py (binary frames)
    node/                   agent.py monitor.py clock.py drivers/{base,noise,sim,sigmf,soapy}.py
    server/                 app.py settings.py hub.py store.py pipeline/ api/ static/
    cli.py
  ui/                       React + TypeScript + Vite source; build output goes to
                            src/petal_locate/server/static/
  tests/                    pytest; test_<module>.py per module, tests/e2e/ for system tests
```

Run tests with `cd locate && .venv/bin/python -m pytest -q`. Long statistical tests are
marked `@pytest.mark.slow` but must still pass.

## 2. Coding rules

- Python 3.11+, full type hints, `from __future__ import annotations`.
- `core/` is pure: functions of their arguments, vectorised with numpy, deterministic
  (every random draw takes an explicit seed, key or `numpy.random.Generator`).
  `core/` reads configuration objects by attribute and never imports
  `petal_locate.config`.
- Docstrings state units and frames for every argument and return value.
- Absolute time is `t_ns: int` (MATH.md section 1).
- No silent fallbacks: invalid input raises `ValueError` naming the field. A result
  that could not be computed is `None` plus a reason, never a guess and never NaN in
  an API payload.
- The asyncio event loop is never blocked for more than 50 ms: `render_iq`,
  `welch_psd_db`, `classify`, `caf_estimate`, `solve`, `lop_geojson`, `accuracy_map`
  and SQLite writes run through `asyncio.to_thread` behind a semaphore (3 on the
  server, 2 per node).
- Dependencies beyond pyproject.toml only when needed (`contourpy` is expected for
  lines of position). Optional hardware libraries are imported lazily.
- Every module ships tests, including the relevant MATH.md section 12 checks, each
  against an independent calculation.

## 3. Configuration model (`config/models.py`)

Pydantic v2 models, `extra="forbid"`. YAML and JSON load to the same models.

```
SystemConfig
  version: int = 1
  name: str, description: str = ""
  dut1_s: float = 0
  satellites: list[Satellite]
  sites: list[Site]
  carriers: list[Carrier] = []          # authorised carriers (frequency plan)
  processing: ProcessingConfig = defaults

Satellite        id, name, norad_id?: int, notes: str = "", orbit: Orbit, transponders: list[Transponder],
                 ephemeris_sigma?: {pos_m: [3], vel_mps: [3]}     # RIC 1-sigma override
Orbit            discriminated on `type`:
                   tle    {line1, line2}
                   geo    {lon_deg, inclination_deg=0, epoch="2026-01-01T00:00:00Z", phase_deg=0, drift_deg_per_day=0}
                   kepler {a_m, e, inc_deg, raan_deg, argp_deg, mean_anomaly_deg, epoch, j2=true}
                   table  {samples: [{t: iso, r_m: [3], v_mps: [3]}]}  (ECEF)
Transponder      id, uplink_center_hz, bandwidth_hz, translation_hz (signed; downlink = uplink + translation),
                 g_over_t_dbk, sfd_dbw_m2, eirp_sat_dbw, group_delay_s=0
Site             id, name, lat_deg, lon_deg, alt_m=0, antenna_height_m=10, clock: {sigma_ns=30},
                 channels: list[ReceiveChannel]
ReceiveChannel   id, name="", antenna: Antenna, pointing: Pointing, system_noise_temp_k=400,
                 polarization_loss_db=0, max_sample_rate_hz=2e6, monitors: list[Monitor] = []
Pointing         {satellite_id}                                  # tracks that satellite; boresight recomputed from its orbit
               | {az_deg, el_deg, satellite_ids: [str, ...]}     # fixed boresight; watches the listed satellites (at least one)
Monitor          id, center_hz, span_hz, nfft=1024, navg=8, frame_rate_hz=2.0       (downlink frequencies)
Antenna          discriminated on `type`:
                   isotropic {gain_dbi=0}
                   parabolic {diameter_m, efficiency=0.55}       # a PETAL dish is this
                   beam      {gain_dbi, hpbw_deg, front_to_back_db=20}   # yagi, helix, patch
                   table     {theta_deg: [...], gain_dbi: [...]}
EmitterModel     eirp_dbw, antenna: Antenna = isotropic, pointing: {satellite_id} | {az_deg, el_deg} | "omni" = "omni",
                 bandwidth_hz, waveform: "cw" | "noise" | "chirp" | "psk" = "noise"
Carrier          id, name, satellite_id, transponder_id, uplink_center_hz, bandwidth_hz,
                 location?: {lat_deg, lon_deg, alt_m=0, antenna_height_m=10}, emitter?: EmitterModel,
                 use_as_reference=false         (use_as_reference requires location and emitter)
ProcessingConfig detection, capture, caf, solver, uncertainty, calibration, region   (all with defaults)
  detection      threshold_db=6, confirm_m=3, confirm_n=5, release_frames=6, merge_bins=2,
                 baseline_tau_s=600, power_anomaly_db=6, min_site_separation_km=200,
                 verdict_settle_s=5, sensitivity_margin_db=6
  capture        duration_s=1.0, lead_s=0.6, interval_s=5.0, oversample=1.25,
                 min_sample_rate_hz=20e3, max_samples=262144, timeout_s=15
  caf            min_snr_db=13, max_tdoa_sigma_s=5e-4, doppler_margin_hz=20, delay_margin_s=20e-6
  solver         height_m=0, min_elevation_deg=0, confidence=0.95, grid_step_deg=1.0, n_starts=16,
                 chi2_ambiguity=9.0, max_measurements=400
  uncertainty    ephemeris: {operator: {pos_m: [100,1000,1000], vel_mps: [0.02,0.02,0.05]},
                             tle:      {pos_m: [2000,20000,10000], vel_mps: [0.3,0.3,0.7]}}   (RIC 1-sigma)
                 sat_osc_frac=1e-6      # translation-oscillator offset, uncalibrated
                 site_osc_frac=1e-11, tdoa_bias_ns=50
  calibration    enabled=true, interval_s=30, max_age_s=300
  region         Region
Region           lat_min=-70, lat_max=70, lon_min=-180, lon_max=180
```

Rules:

- Ids: satellite, site, carrier and channel ids are unique across the whole config
  (channel ids across sites); transponder ids are unique within a satellite and
  monitor ids within a channel.
- Orbits of type `tle` use the `tle` ephemeris sigmas; `geo`, `kepler` and `table` use
  `operator`; `Satellite.ephemeris_sigma` overrides either.
- Monitor: the node reads `nfft * navg` samples at `fs = span_hz` centred on
  `center_hz`. Validation: `span_hz <= max_sample_rate_hz` and
  `nfft * navg / span_hz <= 1 / frame_rate_hz`, else `ValueError` naming the monitor.
  Empty `monitors` makes the loader derive one window per transponder of each watched
  satellite: `center_hz` = downlink centre, `span_hz = min(1.25 * bandwidth_hz,
  max_sample_rate_hz)`, `navg` reduced (minimum 1) until the frame fits the frame
  period; when even `navg = 1` does not fit, `nfft` is halved (minimum 64) until it
  does. A transponder wider than `max_sample_rate_hz` is monitored only over its
  central `max_sample_rate_hz` unless explicit monitors tile it.
- Region: `lon_min = -180` with `lon_max = 180` is the full circle; otherwise the span
  runs eastward from `lon_min` to `lon_max` (`lon_max < lon_min` wraps the
  antimeridian; equal values are invalid). Methods: `contains(lat_deg, lon_deg) ->
  bool array`; `lon_span_deg() -> float`; `grid(step_deg) -> (lat_deg[n_lat],
  lon_deg[n_lon])` with `lon_deg = lon_min + k * step`, increasing and allowed to
  exceed 180 (unwrapped).
- Other validation: references resolve, positive bandwidths, latitude and longitude in
  range, TLE checksum, table strictly increasing in time.

Helper methods on `SystemConfig`: `satellite(id)`, `site(id)`,
`channel(id) -> (Site, ReceiveChannel)`, `site_xyz(site_id) -> ndarray[3]` (phase
centre, includes `antenna_height_m`), `sites_xyz() -> dict`, `orbits() -> dict[sat_id,
Orbit]`, `boresight_ecef(channel_id, t_s) -> unit ECEF vector`,
`channels_watching(sat_id) -> list[(Site, ReceiveChannel)]`,
`transponders_covering(f_lo_up, f_hi_up) -> list[(Satellite, Transponder)]` (every
transponder whose uplink passband overlaps the range at all),
`uplink_for(sat_id, f_down_hz) -> (Transponder, f_up_hz) | None`,
`downlink_hz(sat_id, transponder_id, f_up)`,
`resolved_uncertainty() -> core.solver.Uncertainty`.

`loader.py`: `load_config(path) -> SystemConfig`, `dump_config(cfg) -> str` (YAML),
`config_json_schema() -> dict`.

Node tokens, the API token and the marking banner are **server settings**, not part of
`SystemConfig` (section 7).

Example configs to ship (in `locate/config/`). All sites and reference locations are
generic city-centre coordinates with neutral names; nothing is a real facility.

- `pacific-uhf.yaml` (default): legacy-UHF bent-pipe channels (illustrative 25 kHz
  channels, uplink near 300 MHz) on three satellites: `muos-pac` "MUOS Pacific
  (nominal 177 W)", `uhf-172e` "UFO-class legacy UHF (nominal 172 E)" and `muos-io`
  "MUOS Indo-Asia (nominal 75 E)", inclinations a few degrees. `muos-pac` and
  `muos-io` share translation -41.0 MHz (co-channel downlinks, about 120 degrees apart
  in the sky); `uhf-172e` uses -33.6 MHz. Suggested link numbers: G/T -14 dB/K, SFD
  -136 dBW/m^2, saturated EIRP 26 dBW per channel. Seven sites: Honolulu, Hilo, Tokyo,
  Naha, Seoul, Busan, Manila; each has one channel per visible satellite, with a mix
  of antennas (0.72 m and 1.2 m parabolic, and a 10 dBi beam antenna) so the
  electrically-small case appears. Two reference carriers at known locations (Guam
  and Sapporo city centres), each with an `emitter`. Slots, inclinations, channel
  frequencies and link parameters are illustrative and say so in
  `description`/`notes`. Region lat -60..60, lon 60..-120 (wraps).
- `ku-adjacent.yaml`: three generic Ku-band GEO satellites 2 to 3 degrees apart with
  36 MHz transponders (14 GHz up, about 11.7 to 12.2 GHz down, G/T +3 dB/K, SFD -85
  dBW/m^2, saturated EIRP 50 dBW), two sites with 1.2 m and 2.4 m parabolic channels,
  one reference carrier. Exercises the narrow-separation, wide-band regime.
- `leo-example.yaml`: two satellites defined by Kepler elements in low Earth orbit
  sharing a transponder plan, one site. Used by geometry and solver tests only.

## 4. Simulation (`sim/`)

```
Scenario
  name, seed=1, description=""
  emitters: list[EmitterSpec]
  errors: TruthErrors
EmitterSpec      id, name="", kind: "jammer" | "carrier" | "local", carrier_id?: str,
                 lat_deg, lon_deg, alt_m=0, antenna_height_m=10,
                 freq_hz (RF carrier actually radiated), eirp_dbw (on boresight),
                 antenna: Antenna = isotropic, pointing: {satellite_id} | {az_deg, el_deg} | "omni" = "omni",
                 waveform: WaveformSpec, enabled=true, t_start?: iso, t_stop?: iso
WaveformSpec     type: cw | noise | chirp | psk, bandwidth_hz=0, sweep_period_s=0.01,
                 symbol_rate_hz=0, order=4, rolloff=0.35, duty=1.0, period_s=0, seed=0
TruthErrors      ephemeris: {sat_id: {ric_pos_m: [3], ric_vel_mps: [3], t_ref?: iso}} = {}
                 sat_osc_frac: {sat_id: float} = {}
                 site_clock_offset_ns: {site_id: float} = {}     # delta_g: true time minus labelled time
                 site_osc_frac: {site_id: float} = {}
                 iono: {enabled=false, vtec_tecu=30}
```

For an `EmitterSpec` with `carrier_id`, the fields `lat_deg`, `lon_deg`, `alt_m`,
`freq_hz`, `eirp_dbw`, `antenna`, `pointing` and the waveform are optional and default
to the Carrier's values; explicit values override (for example to simulate a
mis-surveyed reference). `SimWorld` creates an emitter for every config Carrier that
has `location` and `emitter` and that no scenario emitter names.
`Scenario.validate_against(config)` raises on unknown ids.

`SimWorld(config, scenario)`:

- `render_iq(channel_id, t_start_ns, n, fs, fc) -> complex64[n]` implements MATH.md
  section 4 exactly (paths, visibility, channel filter, in-band slices, relayed noise,
  thermal noise keyed so repeat calls agree). Delay and phase come from a polynomial
  fit of the exact light-time functions over the window, fit error checked (< 1e-4
  cycle of carrier phase) and the window subdivided when needed.
- `render_psd(channel_id, t_start_ns, fc, span_hz, nfft, navg) -> float32[nfft]`:
  the expected Welch PSD without synthesising IQ. Linear power in bin k =
  1 (thermal, 0 dB) + sum over transponders of `N0_relay / N0_site` on their passband
  bins + sum over paths of `(C_rx / (N0_site * span_hz)) * nfft * S_p[k]`, where `S_p`
  (sum over k = 1) is the path's spectral shape centred on its `f_bb` (flat over the
  bandwidth for noise and chirp, RRC squared for psk, the Hann main lobe for cw),
  scaled by the gate duty overlapping the snippet; then multiplied per bin by a
  `Gamma(shape=navg, scale=1/navg)` draw keyed by (channel, centre, `t_start_ns`).
  A test requires `render_psd` and `welch_psd_db(render_iq(...))` to agree within
  1 dB (mean over occupied bins and over empty bins).
- `truth_measurement(emitter_id, a: PathSpec, b: PathSpec, t_ns) -> (tdoa, fdoa)` with
  all truth errors applied; `path_budget(emitter_id, sat_id, transponder_id,
  channel_id, t_s) -> PathBudget | None`; `truth_orbit(sat_id)`;
  `emitters_at(t_ns)`.
- `SimWorld` is cheap to rebuild when the scenario changes.

`montecarlo.py`: `run_trials(config, scenario, emitter_id, n_trials, ...) -> dict`
driving render_iq -> caf_estimate -> solve directly (no network), drawing truth errors
from the configured uncertainty when asked, and reporting miss distance statistics,
NEES and ellipse containment.

## 5. Core API (signatures other modules rely on)

Shapes: `x_e` may have leading axes `[..., 3]`; time arguments may be a float or
`ndarray[M]`; results broadcast to `[..., M]` (`[...]` for scalar time).
`orbits` is `dict[sat_id, Orbit]`, `sites_xyz` is `dict[site_id, ndarray[3]]`.

```python
# timebase.py
now_ns() -> int;  ns_from_iso(s: str) -> int;  iso_from_ns(t_ns: int) -> str
sec(t_ns: int) -> float;  gmst_rad(t_s, dut1_s=0.0)

# geodesy.py   (degrees in, metres out)
lla_to_ecef(lat_deg, lon_deg, alt_m=0.0) -> ndarray[...,3]
ecef_to_lla(xyz) -> (lat_deg, lon_deg, alt_m)
enu_basis(lat_deg, lon_deg) -> ndarray[3,3]            # rows east, north, up (unit ECEF vectors)
look_angles(site_xyz, target_xyz) -> (az_deg, el_deg, range_m)
azel_to_ecef(lat_deg, lon_deg, az_deg, el_deg) -> unit ndarray[3]
angle_between_deg(u, v);  rz(angle_rad) -> ndarray[3,3]
geodesic_distance_m(lat1, lon1, lat2, lon2);  destination(lat, lon, bearing_deg, dist_m)
radii_of_curvature(lat_deg) -> (M, N)                  # meridional, prime vertical

# orbits.py
class Orbit(Protocol):  state_ecef(t_s) -> tuple[ndarray, ndarray]
TleOrbit, GeoOrbit, KeplerOrbit, TableOrbit, PerturbedOrbit(base, ric_pos_m, ric_vel_mps, t_ref_s)
orbit_from_config(orbit_model, dut1_s=0.0) -> Orbit     # reads attributes; accepts the pydantic models or any object with the same fields
subsatellite_point(orbit, t_s) -> (lat_deg, lon_deg, alt_m)

# geometry.py
uplink_delay(x_e, orbit, t_sat_s) -> tau_u
downlink_delay(orbit, x_g, t_rx_s) -> tau_d
@dataclass BentPipe: D, tau_d, tau_u, t_sat_s           # D, tau_u: [..., M]; tau_d, t_sat_s: [M]
bent_pipe(x_e, orbit, x_g, t_rx_s, tau_x=0.0) -> BentPipe
bent_pipe_rates(x_e, orbit, x_g, t_rx_s, tau_x=0.0) -> (D_dot, tau_d_dot, tau_u_dot)   # tau_u_dot = d tau_u / d t_sat
baseband_frequency(f_e, translation, f_tune, tau_d_dot, tau_u_dot, eps_g=0.0) -> f_bb
@dataclass(frozen=True) PathSpec: sat_id, site_id, f_e_hz, translation_hz, tau_x_s=0.0
predict_tdoa(x_e, a: PathSpec, b: PathSpec, t_rx_s, orbits, sites_xyz) -> ndarray[..., M]     # MATH section 5, t_rx_s = t_mid
predict_fdoa(x_e, a: PathSpec, b: PathSpec, t_rx_s, orbits, sites_xyz) -> ndarray[..., M]
elevation_deg(x, orbit, t_s) -> ndarray                  # elevation of the satellite seen from ground point(s) x
footprint(orbit, t_s, min_elevation_deg=0.0, n=181) -> list[(lat_deg, lon_deg)]   # closed ring, longitudes continuous around the sub-satellite longitude
measurement_windows(a, b, t_rx_s, orbits, sites_xyz, region, min_elevation_deg=0.0, height_m=0.0, step_deg=2.0)
    -> ((tdoa_min, tdoa_max), (fdoa_min, fdoa_max)) | None      # geometric extremes over the admissible region; None when no admissible point
search_windows(geo_windows, a, b, caf_cfg, uncertainty, correction=None) -> (delay_window, doppler_window)   # MATH section 6 widening
self_peak(sat_id, translation_hz, f_e_hz, a: PathSpec, b: PathSpec, t_rx_s, orbits, sites_xyz) -> (tdoa, fdoa)
    # satellite sat_id heard by both channels: exact CAF location including the tuning offsets of a and b

# antennas.py
gain_dbi(antenna_model, off_axis_deg, freq_hz) -> ndarray
peak_gain_dbi(antenna_model, freq_hz);  hpbw_deg(antenna_model, freq_hz)
electrically_small(antenna_model, freq_hz) -> bool      # parabolic with D < 3 wavelengths
# parabolic: G0 = eta (pi D / lambda)^2; main lobe -12 (theta/HPBW)^2 dB with HPBW = 70 lambda / D degrees,
# floored by the side-lobe envelope min(G0, 32 - 25 log10(theta)) dBi for 1..48 deg and -10 dBi beyond.
# Electrically small (D < 3 lambda): HPBW = min(70 lambda / D, 120), floor G0 - 15 dB. Never below -10 dBi.

# linkbudget.py   (boresight arguments are unit ECEF vectors; None = "omni")
fspl_db(dist_m, freq_hz)
uplink_budget(eirp_dbw, antenna, boresight, x_e, sat_r, f_e_hz, transponder)
    -> UplinkBudget(eirp_toward_sat_dbw, pfd_dbw_m2, cn0_up_dbhz, elevation_deg)      # broadcast over x_e
transponder_outputs(pfd_dbw_m2: ndarray[K], transponder)
    -> TransponderOutputs(eirp_down_dbw: ndarray[K], relayed_n0_eirp_dbw_hz: float, backoff_db: float)
downlink_budget(eirp_down_dbw, relayed_n0_eirp_dbw_hz, f_d_hz, sat_r, site_xyz, channel, boresight)
    -> DownlinkBudget(c_rx_dbw, relayed_n0_dbw_hz, site_n0_dbw_hz, elevation_deg, off_axis_deg)
direct_budget(eirp_dbw, antenna, boresight, x_e, h_e_m, site_xyz, h_g_m, channel, channel_boresight, f_hz)
    -> DirectBudget(c_rx_dbw, in_horizon: bool)
@dataclass PathBudget: uplink, transponder_eirp_dbw, downlink, cn0_total_dbhz;  snr_db(bandwidth_hz)
path_budget(config, emitter: EmitterModel, f_e_hz, x_e, sat_id, transponder_id, channel_id, t_s, other_pfd_dbw_m2=()) -> PathBudget | None
downlink_quality_db(config, channel_id, sat_id, transponder_id, t_s) -> float | None      # MATH section 7; None below the horizon

# iono.py
slant_factor(el_deg);  group_delay_s(vtec_tecu, el_deg, freq_hz)

# crlb.py
effective_snr(g1, g2);  coherence(g1, g2);  post_correlation_snr(noise_bw_hz, T_s, g1, g2) -> rho
stein_sigmas(noise_bw_hz, rms_bw_hz, T_s, g1, g2) -> (sigma_tdoa_s, sigma_fdoa_hz)     # with the q term when rms_bw < B/sqrt(12)
sigmas_from_coherence(C, noise_bw_hz, rms_bw_hz, T_s) -> (sigma_tdoa_s, sigma_fdoa_hz)

# spectrum.py
welch_psd_db(iq: complex[nfft*navg], fs, nfft, navg) -> float32[nfft]     # MATH section 7 normalisation; fft-shifted

# signals.py
class Waveform: evaluate(base_ns: int, t_rel: ndarray) -> complex128;  bandwidth_hz;  rms_bandwidth_hz;  spectral_shape(freqs_hz) -> ndarray
waveform_from_spec(spec, key_label: str) -> Waveform
key128(label: str) -> int

# channel.py
render_path(waveform, base_ns, n, fs, delay_fn, phase_fn, amplitude) -> complex128[n]
    # delay_fn(t_rel: float64[n]) -> seconds, phase_fn(t_rel) -> cycles; t_rel = k/fs seconds of labelled time after base_ns
decimate(x, m) -> ndarray            # linear-phase FIR of MATH section 4, group delay removed exactly
thermal_noise(n, key_label) -> complex128[n]      # E|x|^2 = 1

# caf.py
@dataclass CafResult: tdoa_s|None, fdoa_hz|None, sigma_tdoa_s, sigma_fdoa_hz, rho_tf, snr_db, coherence, detected: bool,
                      mirror_ambiguous: bool, candidates: list[(tdoa_s, fdoa_hz, snr_db)], rms_bandwidth_hz,
                      surface: float32[<=65, <=65] dB, delay_axis_s, doppler_axis_hz,
                      delay_cut: (axis_s, dB), doppler_cut: (axis_hz, dB), reason: str|None
caf_estimate(ya: complex[n], yb: complex[n], fs, *, delay_window: tuple[float, float], doppler_window: tuple[float, float],
             signal_bw_hz: float | None = None, t0_offset_s: float = 0.0, exclude: Sequence[tuple[float, float]] = (),
             mirror_window: tuple[tuple[float, float], tuple[float, float]] | None = None,
             min_snr_db=13.0, max_tdoa_sigma_s=5e-4) -> CafResult

# detect.py
@dataclass Detection: track_id: int, f_lo_hz, f_hi_hz (downlink), power_db, snr_db, cn0_dbhz,
                      first_seen_ns, last_seen_ns, authorized_carrier_id|None, anomaly: bool
class SignalTracker:
    __init__(center_hz, span_hz, nfft, frame_rate_hz, cfg,
             passbands: list[(f_lo_hz, f_hi_hz, relay_rise_db)], carriers: list[(carrier_id, f_lo_hz, f_hi_hz)])
    update(psd_db: float32[nfft], t_ns: int) -> list[Detection]      # every confirmed, unreleased track
    baseline_db: float32[nfft]
classify(iq, fs) -> Classification(label, confidence, features: dict)

# solver.py
@dataclass(frozen=True) CalRef: cal_id: str, x_ref: ndarray[3], a: PathSpec, b: PathSpec, t_ns: int, sigma: float
@dataclass Measurement: kind ("tdoa"|"fdoa"), value, sigma (CAF noise of this measurement only), t_ns (t_mid of MATH section 5),
                        a: PathSpec, b: PathSpec, correction: float = 0.0, cal: CalRef | None = None, tag: str = ""
@dataclass Uncertainty: sat_pos_ric_m: dict[sat_id, ndarray[3]], sat_vel_ric_mps: dict[sat_id, ndarray[3]],
                        site_clock_s: dict[site_id, float], site_osc_frac: dict[site_id, float],
                        sat_osc_frac: dict[sat_id, float], tdoa_bias_s: float
@dataclass Solution: lat_deg, lon_deg, height_m, cov_en_m2 (2x2 total), cov_noise_en_m2, cov_consider_en_m2,
                     ellipse: {semi_major_m, semi_minor_m, azimuth_deg, confidence}, cep50_m,
                     chi2, dof, reduced_chi2: float | None, residuals (whitened, input order),
                     ambiguities: list[Solution], ambiguity_resolved_by: "none"|"region"|"measurements"|"unresolved",
                     converged: bool, dominant_error: str, n_measurements: int
solve(measurements, orbits, sites_xyz, solver_cfg, region, uncertainty: Uncertainty | None = None,
      starts: Sequence[tuple[float, float]] | None = None) -> tuple[Solution | None, str | None]     # (solution, reason)
calibration_correction(ref_measurement: Measurement, x_ref, orbits, sites_xyz, cal_id) -> tuple[float, CalRef]
error_ellipse(P_2x2, confidence) -> dict;  cep50(P_2x2) -> float

# lop.py
lop_geojson(measurement, orbits, sites_xyz, region, height_m=0.0, min_elevation_deg=0.0, step_deg=0.25, k_sigma=2.0) -> dict   # FeatureCollection

# accuracy.py
accuracy_map(config, pairs: list[tuple[str, str]] (channel ids A, B), emitter: EmitterModel, f_e_hz, t_ns, region, step_deg,
             kinds=("tdoa", "fdoa"), reference_carrier_id: str | None = None, integration_s: float | None = None) -> dict
    # keys lat_deg[n_lat], lon_deg[n_lon] (unwrapped), semi_major_95_m[n_lat][n_lon], cep50_m[n_lat][n_lon],
    # dominant[n_lat][n_lon]; null where not admissible or unobservable
```

## 6. Node agent (`node/`)

One process per site. `drivers/base.py`:

```python
@dataclass Capture: iq: complex64[n], t_first_ns: int, fs, fc, overflow: bool, clock_locked: bool
@dataclass Capabilities: min_hz, max_hz, max_sample_rate_hz, lookback_s, concurrent_reads
@dataclass DriverContext: site_id, server_http_url, token, simulation: dict | None

class SdrDriver(Protocol):
    capabilities: Capabilities
    async def open(self, channel: ChannelAssignment, ctx: DriverContext) -> None
    async def read(self, t_start_ns: int, n: int, fs: float, fc: float) -> Capture
        # t_start_ns may be slightly in the future (driver waits) or in the recent past
        # (lookback); raises CaptureError(code, message) when it cannot serve the interval.
    async def psd(self, t_start_ns: int, fc: float, span_hz: float, nfft: int, navg: int) -> ndarray | None
        # float32[nfft] in welch_psd_db units; None when the driver has no fast path
    async def close(self) -> None
```

Monitor loop: call `driver.psd(...)`; if it returns `None`,
`read(t_start_ns, nfft * navg, span_hz, fc)` and apply `welch_psd_db`.

Drivers: `noise` (unit noise; protocol tests), `sim` (a `SimWorld` shared by all sim
drivers of the node, built from the world fetched over the simulation backplane;
`read` returns only when wall-clock time has passed `t_start_ns + n/fs`; lookback 30
s; implements `psd` with `render_psd`), `sigmf` (replays a SigMF recording with its
timestamps), `soapy` (SoapySDR; written against the API but untested on hardware, and
documented as such).

The agent connects (PROTOCOL.md), heartbeats, runs one monitor loop per assigned
window, serves capture requests concurrently, keeps at most one unsent PSD frame per
window, reconnects with exponential backoff and jitter and never crashes on a server
error.

## 7. Server (`server/`)

FastAPI application created by `create_app(settings) -> FastAPI`.

`Settings`: config path, optional scenario path, data directory, host (default
127.0.0.1), port, `node_tokens: dict[site_id, token]` (from `--node-tokens FILE`, a
YAML map, or environment `PETAL_NODE_TOKEN_<SITE_ID>`; a site with no token cannot
connect; `demo` generates random per-run tokens), optional `api_token` (when set,
every `/api` request and the `/ws/ui` handshake must carry it), and optional
`marking: {level, line}` for the console banner (deployment configuration, never
inferred).

- `hub.py`: node sessions, authentication, heartbeats and latency, PSD fan-out,
  `await hub.capture(channel_id, t_start_ns, n, fs, fc, deadline_ns) -> Capture`
  (raises `CaptureFailed(code, message)`), `hub.assign(...)`, per-channel
  capabilities from `assign_ack`, liveness.
- `store.py`: SQLite (stdlib `sqlite3`, WAL). Tables: config versions, events,
  evidence, measurements, solutions, calibrations, node sessions. IQ captures on disk
  under the data directory with a retention limit.
- `pipeline/detection.py`: PSD frames to detections per monitor (`core.detect`).
- `pipeline/correlator.py`: event identity, evidence rows, capability, verdict (MATH
  section 7), raising and closing events.
- `pipeline/tasking.py`: for each active event whose verdict is `uplink` or
  `undetermined`:
  - uplink range `[f_lo, f_hi]`; `f_e_nom = round((f_lo + f_hi) / 2)` Hz;
    `sample_rate_hz = min(max(capture.min_sample_rate_hz, capture.oversample * (f_hi -
    f_lo)), the lower usable maximum of the two channels)`, identical in both
    requests; `center_hz = f_e_nom + translation_hz` of each channel's transponder;
    `n_samples = min(round(capture.duration_s * fs), capture.max_samples)`;
    `t_start_ns = server now + max(capture.lead_s, 3 x the worst heartbeat round trip
    of the nodes involved)`, identical in both requests;
    `deadline_ns = t_start_ns + (n / fs + capture.timeout_s) * 1e9`.
  - Primary channel: the event's detection with the highest `snr_db`. For each other
    satellite whose transponder overlaps the uplink range: the channel watching it
    with the highest `downlink_quality_db`, preferring a channel at the primary's site
    when within 3 dB (common clock). `capture.interval_s` is per event: all its pairs
    share one `t_start` and the primary capture.
  - Windows from `measurement_windows` + `search_windows`; `exclude` from `self_peak`
    for every satellite heard by both channels (and the direct-path point when both
    channels are at one site with equal translations); `mirror_window` when the two
    satellites are co-channel.
  - A pair is rejected (reason recorded) if a returned `sample_rate_hz` differs from
    the request; centre-frequency differences are corrected per MATH section 5. A
    capture whose clock is unlocked yields no TDOA (reason `clock_unlocked`) unless
    both channels are at the same site; its FDOA is still used.
  - Classification from the primary capture. CAF results that are ambiguous, coupled
    or undetected are recorded with their reason and not passed to the solver; a
    periodic waveform is retried with a capture at least 5 sweep periods long.
  - Reference calibrations run only for pairs of active events, every
    `calibration.interval_s`, on the reference carrier's own frequency; a calibration
    is keyed by `(a.sat_id, a.site_id, b.sat_id, b.site_id, kind)`, sign-flipped for
    the reversed pair, valid for `calibration.max_age_s`.
  - Accumulates measurements (newest `solver.max_measurements`), solves (warm start
    and global-search schedule of MATH section 9), stores and publishes.
- `pipeline/advisory.py`: plain-language guidance from the event (uplink: change
  channel, transponder or satellite, moving the terminal will not help; local: only
  that site is affected, relocate, shield or filter; undetermined: what evidence is
  missing).
- `api/`: the console interface of PROTOCOL.md (REST under `/api/v1`, `/ws/ui`).
  FastAPI serves OpenAPI at `/openapi.json`.
- Serves the built UI from `server/static/` at `/`.

Event lifecycle: `detected` (raised) -> `characterizing` (first capture requested) ->
`locating` (first CAF measurement accepted) -> `located` (a converged solution
exists); `lost` (all detections released); `closed` (operator). Every transition is
stored and pushed.

## 8. CLI (`cli.py`)

```
petal-locate serve   --config C [--scenario S] [--host --port --data-dir --node-tokens FILE --api-token T --marking-level L --marking-line TEXT]
petal-locate node    --server URL --site ID --token T --driver sim|sigmf|soapy|noise [--node-id ID] [driver options]
petal-locate demo    [--config C --scenario S --port P]     server plus one `petal-locate node` subprocess per site, over real WebSockets; terminates them on exit
petal-locate check   --config C                            validate, print look angles, monitors and link budgets
petal-locate accuracy --config C ...                        accuracy map to JSON
petal-locate montecarlo --config C --scenario S --trials N  full IQ -> CAF -> solve statistics
```

## 9. What is and is not proven here

Everything runs against a physics simulator. The simulator and the solver share the
geometry code, so the tests prove the signal processing, estimation, protocol and
system logic, and they prove self-consistency of the model, not agreement with real
satellites. `docs/LIMITATIONS.md` must list: illustrative link and frequency-plan
numbers, nominal orbital slots, no real-hardware validation of drivers,
UT1/polar-motion simplifications, linear transponder model, no terrain model,
stationary-emitter assumption, CAF dynamics limits for low orbits, order-of-magnitude
ephemeris sigmas for TLEs, and the requirement that the reference satellite's
transponder overlaps the interferer's uplink band.
