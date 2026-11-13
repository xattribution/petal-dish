# PETAL Locate — architecture and module contracts

PETAL Locate detects interference on bent-pipe satellite transponders, decides whether
it is on the uplink or local to one ground site, and geolocates uplink emitters from
time and frequency differences between copies relayed by two or more satellites.
Nothing is specific to one satellite, band or site: satellites, transponders, sites,
antennas, frequency plans and processing settings are all configuration.

Read `MATH.md` first. It fixes every sign and unit.

## 1. Layout

```
locate/
  pyproject.toml            Python package (venv at locate/.venv)
  config/                   example system configs and simulation scenarios (YAML)
  docs/                     MATH.md, ARCHITECTURE.md, PROTOCOL.md, API.md, RESULTS.md ...
  src/petal_locate/
    core/                   pure numerics, no I/O, no asyncio, no globals
      constants.py timebase.py geodesy.py orbits.py geometry.py
      antennas.py linkbudget.py iono.py
      signals.py channel.py
      caf.py crlb.py detect.py
      solver.py lop.py accuracy.py
    config/                 models.py (pydantic), loader.py
    sim/                    world.py (truth + IQ synthesis), scenario.py, montecarlo.py
    protocol/               messages.py (pydantic), framing.py (binary frames)
    node/                   agent.py monitor.py clock.py drivers/{base,sim,sigmf,soapy,noise}.py
    server/                 app.py hub.py store.py pipeline/ api/ static/
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
  (every random draw takes an explicit seed or `numpy.random.Generator`).
- Docstrings state units and frames for every argument and return value.
- Absolute time is `t_ns: int`. Never store absolute time in float32, and never
  subtract two large float timestamps to get a delay (see MATH.md section 1).
- No silent fallbacks: invalid input raises `ValueError` with a message naming the
  field. A result that could not be computed is `None` plus a reason, never a guess.
- Do not add dependencies beyond pyproject.toml without need; `contourpy` may be added
  for line-of-position contours.
- Every module ships tests, including the relevant MATH.md section 12 checks.
- Keep modules importable without optional hardware libraries (SoapySDR is imported
  lazily inside its driver).

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

Satellite        id, name, norad_id?: int, notes: str = "", orbit: Orbit, transponders: list[Transponder]
Orbit            discriminated on `type`:
                   tle    {line1, line2}
                   geo    {lon_deg, inclination_deg=0, epoch="2026-01-01T00:00:00Z", phase_deg=0, drift_deg_per_day=0}
                   kepler {a_m, e, inc_deg, raan_deg, argp_deg, mean_anomaly_deg, epoch, j2=true}
                   table  {samples: [{t: iso, r_m: [3], v_mps: [3]}]}  (ECEF)
Transponder      id, uplink_center_hz, bandwidth_hz, translation_hz (signed; downlink = uplink + translation),
                 g_over_t_dbk, sfd_dbw_m2, eirp_sat_dbw, group_delay_s=0
Site             id, name, lat_deg, lon_deg, alt_m=0, antenna_height_m=10,
                 clock: {sigma_ns=30, disciplined=true}, node_token?: str, channels: list[ReceiveChannel]
ReceiveChannel   id, name="", antenna: Antenna, pointing: Pointing, system_noise_temp_k=400,
                 polarization_loss_db=0, max_sample_rate_hz=2e6, monitors: list[Monitor] = []
                 (empty monitors => loader derives one window covering every transponder
                  downlink of the pointed satellite)
Monitor          id, center_hz, span_hz, nfft=1024, navg=8, rate_hz=2.0       (downlink frequencies)
Antenna          discriminated on `type`:
                   isotropic {gain_dbi=0}
                   parabolic {diameter_m, efficiency=0.55}     # a PETAL dish is this
                   beam      {gain_dbi, hpbw_deg, front_to_back_db=20}   # yagi, helix, patch
                   table     {theta_deg: [...], gain_dbi: [...]}
Pointing         {satellite_id} | {az_deg, el_deg}
Carrier          id, name, satellite_id, transponder_id, uplink_center_hz, bandwidth_hz,
                 location?: {lat_deg, lon_deg, alt_m=0}, use_as_reference=false
ProcessingConfig detection, capture, caf, solver, uncertainty, calibration, region   (all with defaults)
  detection      threshold_db=6, confirm_m=3, confirm_n=5, release_frames=6, merge_bins=2,
                 baseline_tau_s=600, power_anomaly_db=6, min_site_separation_km=200
  capture        duration_s=1.0, lead_s=0.6, interval_s=5.0, oversample=1.25,
                 min_sample_rate_hz=20e3, timeout_s=15
  caf            min_snr_db=13, max_tdoa_sigma_s=5e-4, doppler_margin_hz=20, delay_margin_s=20e-6
  solver         height_m=0, min_elevation_deg=0, confidence=0.95, grid_step_deg=1.0,
                 chi2_ambiguity=9.0, max_measurements=400
  uncertainty    ephemeris_pos_m=[100,1000,1000] (RIC 1-sigma), ephemeris_vel_mps=[0.02,0.02,0.05],
                 clock_ns=30, sat_osc_frac=1e-8, tdoa_bias_ns=50
  calibration    enabled=true, interval_s=30, max_age_s=300
  region         lat_min=-70, lat_max=70, lon_min=-180, lon_max=180   (lon range may wrap: min > max)
```

Validation: unique ids, references resolve, positive bandwidths, monitors non-empty
after derivation, latitude and longitude in range, TLE checksum, table strictly
increasing in time. Helper methods on `SystemConfig`: `satellite(id)`, `site(id)`,
`channel(id) -> (Site, ReceiveChannel)`, `transponders_covering(f_lo_up, f_hi_up)`,
`downlink_hz(sat_id, transponder_id, f_up)`, `channels_pointed_at(sat_id)`.

`loader.py`: `load_config(path) -> SystemConfig`, `dump_config(cfg) -> str` (YAML),
`config_json_schema() -> dict`.

Example configs to ship (in `locate/config/`):

- `pacific-uhf.yaml` (default): legacy-UHF bent-pipe channels on a MUOS Pacific
  satellite (nominal 177 W), a UFO-class satellite (nominal 172 E) and the MUOS
  Indo-Asia satellite (nominal 75 E); sites on Oahu, Hawaii Island, Okinawa, Tokyo
  area, Pyeongtaek and Manila; two reference carriers at known locations (Guam and
  Yokosuka area). Slots, inclinations, channel frequencies and link parameters are
  illustrative and say so in `notes`/`description`. Satellites at 177 W and 75 E share
  the same translation (co-channel downlinks, about 120 degrees apart in the sky); the
  172 E satellite uses a different translation.
- `ku-adjacent.yaml`: three generic Ku-band GEO satellites 2 to 3 degrees apart with
  36 MHz transponders (14 GHz up, 11.7 to 12.2 GHz down), two sites with 1.2 m
  parabolic channels. Exercises the narrow-separation, wide-band regime.
- `leo-example.yaml`: two satellites defined by Kepler elements in low Earth orbit
  sharing a transponder plan, one site. Used by geometry and solver tests only.

## 4. Simulation scenario (`sim/scenario.py`)

```
Scenario
  name, seed=1
  emitters: list[EmitterSpec]
  errors: TruthErrors
EmitterSpec      id, name, kind: "jammer" | "carrier" | "local", carrier_id?: str,
                 lat_deg, lon_deg, alt_m=0, antenna_height_m=10,
                 freq_hz (RF carrier actually radiated), eirp_dbw (on boresight),
                 antenna: Antenna, pointing: {satellite_id} | {az_deg, el_deg} | "omni",
                 waveform: WaveformSpec, enabled=true, t_start?: iso, t_stop?: iso
WaveformSpec     type: cw | noise | chirp | psk, bandwidth_hz=0, sweep_period_s=0.01,
                 symbol_rate_hz=0, order=4, rolloff=0.35, duty=1.0, period_s=0, seed=0
TruthErrors      ephemeris: {sat_id: {ric_pos_m: [3], ric_vel_mps: [3]}} = {}
                 sat_osc_frac: {sat_id: float} = {}
                 site_clock_offset_ns: {site_id: float} = {}
                 site_freq_frac: {site_id: float} = {}
                 iono: {enabled=false, vtec_tecu=30}
```

`SimWorld(config, scenario)`:

- `render_iq(channel_id, t_start_ns, n, fs, fc) -> complex64[n]` implements MATH.md
  section 4 exactly: every emitter through every satellite transponder whose passband
  contains it and whose downlink lands in `[fc - fs/2, fc + fs/2]`, direct paths,
  relayed transponder noise, site thermal noise (seeded by channel and time so repeat
  calls agree). Delay and phase come from a polynomial fit of the exact light-time
  functions over the window, with the fit error checked (< 1e-4 cycle of carrier
  phase) and the window subdivided when needed.
- `truth_measurement(emitter_id, path_a, path_b, t_ns) -> (tdoa, fdoa)` with all truth
  errors applied; `path_budget(...)`; `truth_orbit(sat_id)`.
- A scenario can change at run time; `SimWorld` is cheap to rebuild.

## 5. Core API (signatures other modules rely on)

```python
# timebase.py
now_ns() -> int;  ns_from_iso(s: str) -> int;  iso_from_ns(t_ns: int) -> str
sec(t_ns: int) -> float;  gmst_rad(t_s, dut1_s=0.0)

# geodesy.py   (degrees in, metres out)
lla_to_ecef(lat_deg, lon_deg, alt_m=0.0) -> ndarray[...,3]
ecef_to_lla(xyz) -> (lat_deg, lon_deg, alt_m)
enu_basis(lat_deg, lon_deg) -> ndarray[3,3]            # rows east, north, up (unit ECEF vectors)
look_angles(site_xyz, target_xyz) -> (az_deg, el_deg, range_m)
angle_between_deg(u, v);  rz(angle_rad) -> ndarray[3,3]
geodesic_distance_m(lat1, lon1, lat2, lon2);  destination(lat, lon, bearing_deg, dist_m)

# orbits.py
class Orbit(Protocol):  state_ecef(t_s) -> tuple[ndarray, ndarray]
TleOrbit, GeoOrbit, KeplerOrbit, TableOrbit, PerturbedOrbit
orbit_from_config(orbit_model, dut1_s=0.0) -> Orbit
subsatellite_point(orbit, t_s) -> (lat_deg, lon_deg, alt_m)

# geometry.py
uplink_delay(x_e, orbit, t_sat_s) -> tau_u
downlink_delay(orbit, x_g, t_rx_s) -> tau_d
@dataclass BentPipe: D, tau_d, tau_u, t_sat_s           # arrays shaped like t_rx_s
bent_pipe(x_e, orbit, x_g, t_rx_s, tau_x=0.0) -> BentPipe
bent_pipe_rates(x_e, orbit, x_g, t_rx_s, tau_x=0.0) -> (D_dot, tau_d_dot, tau_u_dot)
baseband_frequency(f_e, translation, f_tune, tau_d_dot, tau_u_dot, eps_g=0.0) -> f_bb
@dataclass(frozen=True) PathSpec: sat_id, site_id, f_e_hz, translation_hz, tau_x_s=0.0
predict_tdoa(x_e, a: PathSpec, b: PathSpec, t_rx_s, orbits, sites_xyz) -> float
predict_fdoa(x_e, a: PathSpec, b: PathSpec, t_rx_s, orbits, sites_xyz) -> float
elevation_deg(x, orbit, t_s);  off_boresight_deg(x_from, boresight_target, other_target)
footprint(orbit, t_s, min_elevation_deg=0.0, n=181) -> list[(lat_deg, lon_deg)]
measurement_windows(a, b, t_rx_s, orbits, sites_xyz, region, min_elevation_deg) -> ((tdoa_min, tdoa_max), (fdoa_min, fdoa_max))
self_peak(a: PathSpec, b: PathSpec, sat_id, t_rx_s, orbits, sites_xyz) -> (tdoa, fdoa)   # same satellite seen by both channels

# antennas.py
gain_dbi(antenna_model, off_axis_deg, freq_hz) -> ndarray
peak_gain_dbi(antenna_model, freq_hz);  hpbw_deg(antenna_model, freq_hz)
electrically_small(antenna_model, freq_hz) -> bool      # parabolic with D < 3 wavelengths

# linkbudget.py
fspl_db(dist_m, freq_hz)
@dataclass PathBudget: eirp_toward_sat_dbw, cn0_up_dbhz, pfd_dbw_m2, eirp_down_dbw, relayed_n0_eirp_dbw_hz,
                       c_rx_dbw, relayed_n0_dbw_hz, site_n0_dbw_hz, cn0_total_dbhz, snr_db(bandwidth_hz)
uplink_budget(...);  transponder_outputs(carriers_pfd_dbw_m2, transponder) ;  downlink_budget(...)

# signals.py
class Waveform: evaluate(base_ns: int, t_rel: ndarray) -> complex128;  bandwidth_hz;  rms_bandwidth_hz
waveform_from_spec(spec) -> Waveform

# channel.py
render_path(waveform, base_ns, n, fs, delay_fn, phase_fn, amplitude) -> complex128[n]
thermal_noise(n, seed_tuple) -> complex128[n]

# caf.py
@dataclass CafResult: tdoa_s|None, fdoa_hz|None, sigma_tdoa_s, sigma_fdoa_hz, snr_db, detected: bool,
                      mirror_ambiguous: bool, rms_bandwidth_hz, surface: ndarray, delay_axis_s, doppler_axis_hz,
                      delay_cut, doppler_cut, reason: str|None
caf_estimate(ya, yb, fs, *, delay_window, doppler_window, t0_offset_s=0.0, exclude=(), mirror_window=None,
             min_snr_db=13.0, max_tdoa_sigma_s=5e-4) -> CafResult
# t0_offset_s = (first-sample time of A) - (first-sample time of B)

# crlb.py
effective_snr(g1, g2);  stein_sigmas(noise_bw_hz, rms_bw_hz, T_s, g1, g2) -> (sigma_tdoa_s, sigma_fdoa_hz)

# detect.py
welch_psd_db(iq, fs, nfft, navg) -> float32[nfft]       # fftshifted, bin k at fc + (k - nfft/2) fs/nfft
class BaselineTracker;  class SignalTracker           # per monitor window
@dataclass Detection: f_lo_hz, f_hi_hz, power_db, snr_db, first_seen_ns, last_seen_ns, authorized_carrier_id|None, anomaly: bool
classify(iq, fs) -> Classification(label, confidence, features: dict)

# solver.py
@dataclass Measurement: kind ("tdoa"|"fdoa"), value, sigma, t_ns, a: PathSpec, b: PathSpec, correction=0.0, tag=""
@dataclass Solution: lat_deg, lon_deg, height_m, cov_en_m2 (2x2 total), cov_noise_en_m2, cov_consider_en_m2,
                     ellipse: {semi_major_m, semi_minor_m, azimuth_deg, confidence}, cep50_m,
                     chi2, dof, residuals, ambiguities: list[Solution], converged, dominant_error: str
solve(measurements, orbits, sites_xyz, solver_cfg, region, uncertainty=None, reference=None) -> Solution
calibration_correction(ref_measurement, x_ref, orbits, sites_xyz) -> (correction, extra_sigma)
error_ellipse(P_2x2, confidence) -> dict

# lop.py
lop_geojson(measurement, orbits, sites_xyz, region, step_deg=0.25, k_sigma=2.0) -> dict   # FeatureCollection

# accuracy.py
accuracy_map(config, pairs, emitter_assumption, t_ns, region, step_deg, use_reference: bool) -> dict  # grid + metrics
```

`orbits` is `dict[sat_id, Orbit]`, `sites_xyz` is `dict[site_id, ndarray[3]]`.

## 6. Node agent (`node/`)

One process per site. `drivers/base.py` defines

```python
class SdrDriver(Protocol):
    async def open(self, channel_cfg) -> None
    async def read(self, t_start_ns: int, n: int, fs: float, fc: float) -> Capture
        # Capture(iq: complex64[n], t_first_ns: int, fs, fc, overflow: bool, clock_locked: bool)
        # t_start_ns may be slightly in the future (driver waits) or in the recent past
        # (ring buffer); raises CaptureError when it cannot serve the interval.
    async def close(self) -> None
    capabilities: {min_hz, max_hz, max_sample_rate_hz, lookback_s}
```

Drivers: `sim` (asks a `SimWorld` built from the scenario fetched from the server's
simulation API), `noise` (unit noise, for protocol tests), `sigmf` (replays a SigMF
recording with its timestamps), `soapy` (SoapySDR; written against the API but
untested on hardware, and documented as such).

The agent connects to the server (PROTOCOL.md), sends heartbeats, runs one monitor
loop per assigned window (read a short snippet, `welch_psd_db`, send a PSD frame),
and serves capture requests concurrently. It reconnects with exponential backoff and
jitter and never crashes on a server error.

## 7. Server (`server/`)

FastAPI application created by `create_app(settings) -> FastAPI`.

- `hub.py`: node sessions, authentication, heartbeats and latency, PSD fan-out,
  `await hub.capture(channel_id, t_start_ns, n, fs, fc) -> Capture`.
- `store.py`: SQLite (stdlib `sqlite3`, WAL). Tables: config versions, events,
  observations, measurements, solutions, calibrations, node sessions. IQ captures on
  disk under the data directory with a retention limit.
- `pipeline/detection.py`: PSD frames to detections per monitor (`core.detect`).
- `pipeline/correlator.py`: associates detections into events, decides uplink / local /
  undetermined, raises and closes events.
- `pipeline/tasking.py`: for each active uplink event, plans pairs (primary channel
  plus one channel per other satellite whose transponder overlaps the uplink band),
  requests synchronised captures, runs classification and the CAF with geometry
  windows, self-peak exclusion and mirror windows, applies calibration, accumulates
  measurements, solves, stores and publishes. Runs the reference-carrier calibration
  schedule.
- `pipeline/advisory.py`: plain-language guidance from the event (uplink: change
  channel, transponder or satellite, moving the terminal will not help; local: affects
  only that site, relocate, shield or filter).
- `api/`: REST for config (get, validate, replace with optimistic versioning), status,
  events, geometry (sub-satellite points, footprints, look angles), analysis (accuracy
  map, lines of position, link budget), simulation control (only when a scenario is
  loaded), and `/ws/ui` pushing status, PSD frames, events and solutions.
  The server agent writes `docs/API.md` and FastAPI serves OpenAPI at `/openapi.json`.
- Serves the built UI from `server/static/` at `/`.

Event lifecycle: `detected -> characterizing -> locating -> located`, and `lost`
(signal gone) or `closed` (operator). Every transition is stored and pushed.

## 8. CLI (`cli.py`)

```
petal-locate serve   --config C [--scenario S] [--host --port --data-dir]
petal-locate node    --server URL --site ID --token T --driver sim|sigmf|soapy|noise [driver options]
petal-locate demo    [--config C --scenario S --port P]     server plus one simulated node per site, all over real WebSockets
petal-locate check   --config C                            validate, print look angles and link budgets
petal-locate accuracy --config C ...                        accuracy map to JSON / GeoJSON
petal-locate montecarlo --config C --scenario S --trials N  full IQ -> CAF -> solve statistics
```

## 9. What is and is not proven here

Everything runs against a physics simulator. The simulator and the solver share the
geometry code, so the tests prove the signal processing, estimation, protocol and
system logic, and they prove self-consistency of the model, not agreement with real
satellites. `docs/LIMITATIONS.md` must list: illustrative link and frequency-plan
numbers, no real-hardware validation of drivers, UT1/polar-motion simplifications,
linear transponder model, no terrain model, stationary-emitter assumption, CAF
dynamics limits for low orbits, and the requirement that the reference satellite's
transponder overlaps the interferer's uplink band.
