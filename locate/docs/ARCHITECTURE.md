# PETAL Locate — architecture and module contracts

PETAL Locate detects interference on bent-pipe satellite transponders, decides whether
it is on the uplink or local to one ground site, and geolocates uplink emitters from
time and frequency differences between copies relayed by two or more satellites.
Nothing is specific to one satellite, band or site: satellites, transponders, sites,
antennas, frequency plans and processing settings are all configuration.

Read `MATH.md` first. It fixes every sign, unit and algorithmic requirement.
Revision 5: matches the implementation after the fixes of the adversarial review
(propagation media and shared capture noise in the solver, the fit-consistency gate,
another transmitter on the frequency, the request guard, retention, per-channel node
options, the fixed simulated epoch), including the optional arguments and helpers other
modules use, the server pipeline's rules (section 7) and the simulator's and Monte Carlo
driver's (section 4). What the proof of concept does not show is in `LIMITATIONS.md`;
how to deploy it in `OPERATIONS.md`.

## 1. Layout

```
locate/
  README.md                 what it is, quick start, map of the documents
  THIRD_PARTY.md            fonts and libraries bundled in the console build, with their full license texts
                            (pyproject license-files: shipped in the wheel's dist-info/licenses/)
  pyproject.toml            Python package (venv at locate/.venv): dependencies, the petal-locate script,
                            the console build as package data
  setup.py                  build step pyproject cannot express: copies config/*.yaml|yml|json into the
                            wheel as petal_locate/data/config/ (where cli.data_path looks when installed;
                            an editable install reads config/ in place) and empties build/lib first
  MANIFEST.in               sdist contents: config/ and THIRD_PARTY.md
  config/                   example system configs; config/scenarios/ the simulation scenarios (presets)
  docs/                     MATH.md ARCHITECTURE.md PROTOCOL.md UI-BRIEF.md CONTRIBUTING.md
                            RESULTS.md (simulated capability study) LIMITATIONS.md OPERATIONS.md
                            results/*.json (the study's raw numbers), images/ (console screenshots)
  scripts/                  capability_study.py (reproduces docs/results)
  src/petal_locate/
    core/                   pure numerics: no I/O, no asyncio, no globals, never imports config/
      constants.py types.py (PathSpec, CalRef, Measurement, Uncertainty; re-exported by geometry.py and solver.py)
      timebase.py geodesy.py orbits.py geometry.py
      antennas.py linkbudget.py iono.py media.py crlb.py spectrum.py
      signals.py channel.py
      caf.py detect.py
      solver.py lop.py accuracy.py
    config/                 models.py (pydantic), loader.py
    sim/                    scenario.py, world.py (truth + IQ/PSD synthesis), montecarlo.py
    protocol/               messages.py (pydantic), framing.py (binary frames)
    node/                   agent.py monitor.py clock.py drivers/{base,noise,sim,sigmf,soapy}.py
    server/                 app.py settings.py hub.py runtime.py store.py static/
      pipeline/             detection.py correlator.py events.py tasking.py advisory.py
      api/                  schemas.py (console contract) rest.py ws.py deps.py
    cli.py
  ui/                       React + TypeScript + Vite source; build output goes to
                            src/petal_locate/server/static/ (committed, so the server needs no Node.js)
  tests/                    pytest; test_<module>.py per module, tests/e2e/ for system tests
                            (test_node_hub.py, test_system.py: the real demo, test_console.py: Playwright;
                            both start the demo with --start 2026-11-13T03:00:00Z, so outcomes do not depend
                            on the time of day)
```

Run tests with `cd locate && .venv/bin/python -m pytest -q`. Long statistical tests and
the end-to-end tests are marked `@pytest.mark.slow` but must still pass
(`-m "not slow"` skips them).

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
  run in worker threads (the server's `WorkPool` of 3 threads, 2 per node); the
  server's tracker updates run on one dedicated spectrum thread and its SQLite writes
  on the store's own writer thread (section 7). The GIL still lets worker threads delay
  the loop: measured worst lag 40 to 66 ms with the seven-node demo on a shared 4-core
  machine (the runtime's loop probe, section 7).
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
                 channels: list[ReceiveChannel], time_zone?: str
                 # time_zone: IANA name (checked with zoneinfo; a well-formed name is accepted when the host
                 # has no tz database), display only (the console's clock strip), left out of exports when unset
ReceiveChannel   id, name="", antenna: Antenna, pointing: Pointing, system_noise_temp_k=400,
                 polarization_loss_db=0, max_sample_rate_hz=2e6, monitors: list[Monitor] = []
Pointing         {satellite_id}                                  # tracks that satellite; boresight recomputed from its orbit
               | {az_deg, el_deg, satellite_ids: [str, ...]}     # fixed boresight; watches the listed satellites (at least one)
Monitor          id, center_hz, span_hz, nfft=1024 (16..131072), navg=8, frame_rate_hz=2.0   (downlink frequencies)
Antenna          discriminated on `type`:
                   isotropic {gain_dbi=0}
                   parabolic {diameter_m, efficiency=0.55}       # a PETAL dish is this
                   beam      {gain_dbi, hpbw_deg, front_to_back_db=20}   # yagi, helix, patch
                   table     {theta_deg: [...], gain_dbi: [...]}
EmitterModel     eirp_dbw, antenna: Antenna = isotropic, pointing: {satellite_id} | {az_deg, el_deg} | "omni" = "omni",
                 bandwidth_hz (0 only for cw), waveform: "cw" | "noise" | "chirp" | "psk" = "noise"
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
                 chi2_ambiguity=9.0, max_measurements=400,
                 max_reduced_chi2=5.0 (> 1; the server's fit-consistency limit, section 7; core.solver ignores it)
  uncertainty    ephemeris: {operator: {pos_m: [100,1000,1000], vel_mps: [0.02,0.02,0.05]},
                             tle:      {pos_m: [2000,20000,10000], vel_mps: [0.3,0.3,0.7]}}   (RIC 1-sigma)
                 sat_osc_frac=1e-6      # translation-oscillator offset, uncalibrated
                 site_osc_frac=1e-11, tdoa_bias_ns=50
                 iono_vtec_tecu=30 (>= 0), iono_correlation=0.5 (0..1)   # one-sigma VTEC per ground end, MATH section 9
                 tropo_zenith_m=2.4 (>= 0), tropo_correlation=0.9 (0..1) # one-sigma zenith delay per ground end
                 # no media delay is applied, so each sigma stands for the whole delay; 0 turns that medium off
  calibration    enabled=true, interval_s=30, max_age_s=300
  region         Region
Region           lat_min=-70, lat_max=70, lon_min=-180, lon_max=180
```

Rules:

- Ids: 1 to 64 characters from `A-Z a-z 0-9 . _ -`, starting with a letter or digit
  (ids go into `|`-separated hash labels, URLs, environment-variable names and file
  names); monitor ids also allow `:` and up to 129 characters (derived monitors are
  named `<satellite id>:<transponder id>`). Satellite, site, carrier and channel ids
  are each unique within their kind across the whole config (channel ids across
  sites; a site may share an id with a satellite); transponder ids are unique within a
  satellite and monitor ids within a channel.
- Orbits of type `tle` use the `tle` ephemeris sigmas; `geo`, `kepler` and `table` use
  `operator`; `Satellite.ephemeris_sigma` overrides either.
- Monitor: the node reads `nfft * navg` samples at `fs = span_hz` centred on
  `center_hz`. Validation: `span_hz <= max_sample_rate_hz`,
  `nfft * navg / span_hz <= 1 / frame_rate_hz` and `16 <= nfft <= 131072` (the
  spectrum tracker refuses fewer than 16 bins, although the wire allows 2), and
  `processing.detection.baseline_tau_s` must span at least 10 frame periods of every
  monitor (the tracker refuses less; the error names the monitor's `frame_rate_hz`, or
  `baseline_tau_s` once for derived monitors). Empty `monitors` gives one derived
  window per transponder of each watched satellite: `center_hz` = downlink centre,
  `span_hz = min(1.25 * bandwidth_hz, max_sample_rate_hz)`, `frame_rate_hz = 2`;
  `nfft` is halved (minimum 64) until the frame fits the frame period, keeping
  `navg = 8`, and only when 64 x 8 samples do not fit (spans under 1024 Hz) is `navg`
  reduced (minimum 1; `ValueError` when even 64 x 1 does not fit). Reason: the
  detection threshold and the C/N0 estimate are calibrated for `navg >= 8`, while a
  coarser bin costs a signal narrower than a bin 3 dB per halving. Watched satellites
  that share a downlink plan would give identical windows; each distinct
  `(center_hz, span_hz)` is monitored once, under the first satellite. Derivation runs
  in `SystemConfig` validation (so a config built from JSON by the API gets monitors
  too) and replaces the channel by a copy; derived monitors are flagged
  (`ReceiveChannel.monitors_derived`) and left out by `dump_config` and `export_dict`
  so that they derive again on load (plain `model_dump` writes them as explicit
  monitors). A transponder wider than `max_sample_rate_hz` is monitored only over its
  central `max_sample_rate_hz` unless explicit monitors tile it (`warnings()` names the
  blind bands); derivation does not drop windows that do not fit one tuning
  (`warnings()` names them too).
- Region: `lon_min = -180` with `lon_max = 180` is the full circle; otherwise the span
  runs eastward from `lon_min` to `lon_max` (`lon_max < lon_min` wraps the
  antimeridian; equal values, and `lon_min = 180` with `lon_max = -180`, are invalid).
  Methods: `contains(lat_deg, lon_deg) -> bool array` (longitudes in any 360 degree
  range); `lon_span_deg() -> float`; `is_full_circle`; `grid(step_deg) ->
  (lat_deg[n_lat], lon_deg[n_lon])` with `lon_deg = lon_min + k * step`, increasing and
  allowed to exceed 180 (unwrapped).
- Other validation (each error names the field): references resolve, positive
  bandwidths, latitude and longitude in range, TLE checksum and matching catalogue
  numbers, table strictly increasing in time, every orbit buildable, a carrier's centre
  inside its transponder's uplink passband, `emitter.bandwidth_hz` not above the
  carrier's, Kepler perigee above the Earth's surface, transponder uplink and downlink
  passbands above 0 Hz, `confirm_m <= confirm_n`, `dut1_s` in [-1, 1], pointing
  `az_deg` in [-360, 360]; unknown fields and non-finite numbers are rejected, and so is
  any string or key holding an unpaired UTF-16 surrogate (`<path>: text that is not
  valid Unicode (an unpaired surrogate)`).
- The orbit and channel-pointing unions are the classes `OrbitModel` and
  `ChannelPointing`, exported also under the names `Orbit` and `Pointing` used above.

Helper methods on `SystemConfig`: `satellite(id)`, `site(id)`,
`channel(id) -> (Site, ReceiveChannel)`, `carrier(id)`, `reference_carriers()`,
`carrier_xyz(carrier_id)`, `site_xyz(site_id) -> ndarray[3]` (phase centre, includes
`antenna_height_m`), `sites_xyz() -> dict`, `orbit(sat_id) -> Orbit`,
`orbits() -> dict[sat_id, Orbit]` (`tle`, `geo` and `kepler` orbits are built on every
call; a `table` orbit is built at validation and reused while its samples' content is
unchanged), `boresight_ecef(channel_id, t_s) -> unit ECEF vector`,
`channels_watching(sat_id) -> list[(Site, ReceiveChannel)]`,
`transponders_covering(f_lo_up, f_hi_up) -> list[(Satellite, Transponder)]` (every
transponder whose uplink passband overlaps the range at all: more than an edge for a
range of non-zero width, the closed passband for a single frequency),
`uplink_for(sat_id, f_down_hz) -> (Transponder, f_up_hz) | None` (the nearest downlink
centre when passbands overlap, the earlier transponder on a tie),
`downlink_hz(sat_id, transponder_id, f_up)`,
`resolved_uncertainty() -> core.solver.Uncertainty`,
`export_dict() -> dict` (JSON-compatible data that loads back to an equal model,
without unset optional fields and derived monitors), and
`warnings(t_s=None) -> list[{loc, msg}]`, what is valid but deserves attention: a
parabolic antenna under 3 wavelengths (one note per channel at its lowest working
frequency, the downlink centres of its watched transponders and its monitor centres;
a carrier emitter's dish at the carrier's uplink centre); a carrier allocation reaching
beyond its transponder's uplink passband; a monitor with `navg < 8`; a channel whose
configured monitors miss the downlink of a transponder it watches; a watched
transponder whose downlink the channel's windows (derived or configured) cover by less
than `MONITOR_COVERAGE_WARN` (0.9), with the percentage and the blind bands (loc
`sites.S.channels.C.monitors`); a channel whose windows span more than
`max_sample_rate_hz` from the lowest edge to the highest, naming the windows a tuning at
the middle would lose; and, with `t_s` (Unix seconds; `ValueError` beyond
`MAX_UNIX_S`), a watched satellite at or below a site's horizon, a satellite whose orbit
cannot be evaluated then (a table not covering `t_s`; its channels then get no horizon
note) and a TLE whose epoch lies more than `TLE_AGE_WARN_DAYS` (14) days from `t_s`, or
`TLE_AGE_WARN_DAYS_FAST` (3) when its mean motion exceeds `TLE_FAST_REV_PER_DAY` (2 rev
per day) (loc `satellites.K.orbit`; the TLE sigmas are not scaled with age). The four
constants are exported; `models.tle_epoch_s(line1)` reads a TLE epoch. Also
`Transponder.relays(f_up_hz)` (centre inside the closed uplink passband) with
`uplink_lo_hz`, `uplink_hi_hz`, `downlink_center_hz`, `downlink_lo_hz`,
`downlink_hi_hz`; `ReceiveChannel.watched_satellite_ids`, `monitors_derived`,
`antenna_summary`; `Monitor.frame_duration_s`, `bin_hz`.

`field_errors(exc) -> list[{loc, msg}]` turns a `ValidationError` into dotted document
paths (pydantic's union-member names, as in `orbit.geo.lon_deg`, removed).

`loader.py`: `load_config(path) -> SystemConfig`,
`loads_config(text, fmt="yaml", source="<string>") -> SystemConfig`,
`dump_config(cfg) -> str` (YAML of `export_dict`), `config_json_schema() -> dict`,
`safe_yaml_load(text)` (a unique-key safe loader that also refuses recursive aliases,
nesting too deep for the parser and documents whose expansion with aliases followed
exceeds `MAX_YAML_NODES = 1_000_000` nodes, raising `YamlLimitError`, a
`yaml.YAMLError`; used for configurations, scenarios, node-token files, accuracy
request files and node files) and `unicode_problem(data) -> {loc, msg} | None`. Every
read, parse or validation failure is a `ConfigError` (a `ValueError`) naming the file,
with `errors` holding the `field_errors` list and the pydantic error as `__cause__`;
duplicate keys in YAML or JSON are rejected; a YAML limit gives `<source>: refused:
<reason>` with `errors [{loc: "(root)", msg: <reason>}]`, and JSON nested too deeply
`[{loc: "(root)", msg: "the document is nested too deeply"}]`.

Node tokens, the API token and the marking banner are **server settings**, not part of
`SystemConfig` (section 7).

Example configs to ship (in `locate/config/`). All sites and reference locations are
generic city-center coordinates with neutral names (`<City> (city center)`); nothing is
a real facility.

- `pacific-uhf.yaml` (default): legacy-UHF bent-pipe channels (illustrative 25 kHz
  channels, uplink near 300 MHz) on three satellites: `muos-pac` "MUOS Pacific
  (nominal 177 W)", `uhf-172e` "UFO-class legacy UHF (nominal 172 E)" and `muos-io`
  "MUOS Indo-Asia (nominal 75 E)", inclinations a few degrees. `muos-pac` and
  `muos-io` share translation -41.0 MHz (co-channel downlinks, about 120 degrees apart
  in the sky); `uhf-172e` uses -33.6 MHz. Suggested link numbers: G/T -14 dB/K, SFD
  -136 dBW/m^2, saturated EIRP 26 dBW per channel. Seven sites: Honolulu, Hilo, Tokyo,
  Naha, Seoul, Busan, Manila; each has one channel per visible satellite, with a mix
  of antennas (0.72 m and 1.2 m parabolic, and a 10 dBi beam antenna) so the
  electrically-small case appears (both dish sizes are under 3 wavelengths at the
  260 to 268 MHz downlinks; `warnings()` gives 13 notes). Two reference carriers at
  known locations, `ref-guam` (shared-1, 300.9925 MHz) and `ref-sapporo` (shared-2,
  301.1175 MHz) at the Guam and Sapporo city centers, each 8 kHz of noise at 20 dBW from
  an omnidirectional antenna parked at the low edge of its shared channel, so that all
  three satellites relay them at full EIRP (the YAML comments give the design and its
  measured numbers; an earlier design with a 10 dBi beam aimed at one satellite gave 2
  to 4 us of reference TDOA noise). Three channels (`solo-*`) are relayed by one
  satellite each: detectable, never locatable. Slots, inclinations, channel
  frequencies and link parameters are illustrative and say so in
  `description`/`notes`. Every site sets `time_zone` (Pacific/Honolulu, Asia/Tokyo,
  Asia/Seoul, Asia/Manila). Region lat -60..60, lon 60..-120 (wraps).
- `ku-adjacent.yaml`: three generic Ku-band GEO satellites 2 to 3 degrees apart with
  one 36 MHz transponder each (14 GHz up, about 11.7 to 12.2 GHz down, G/T +3 dB/K, SFD
  -85 dBW/m^2, saturated EIRP 50 dBW), two sites with 1.2 m and 2.4 m parabolic channels
  whose radios sample up to 45 MHz (`max_sample_rate_hz: 45e6`), so each channel's one
  derived window (45 MHz) covers its whole transponder within one tuning; one reference
  carrier. Exercises the narrow-separation, wide-band regime.
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
                 tropo: {enabled=false, zenith_m=2.3}     # TropoError: relay and relayed-noise legs, envelope and phase
                                                          # delayed alike (FDOA -f d/dt), Black-Eisner mapping; the direct
                                                          # terrestrial path gets none. One value at every ground end, as iono.
```

`loads_scenario` reads YAML with `config.loader.safe_yaml_load` (duplicate keys refused,
alias expansion and nesting bounded: `(root): the document expands to more than 1000000
nodes ...`, `(root): the document is nested too deeply`) and refuses text holding an
unpaired surrogate, as the configuration does. `TropoError` is in
`sim.scenario.__all__` (not re-exported by `sim/__init__.py`).

For an `EmitterSpec` with `carrier_id`, the fields `lat_deg`, `lon_deg`, `alt_m`,
`freq_hz`, `eirp_dbw`, `antenna`, `pointing` and the waveform are optional and default
to the Carrier's values; explicit values override (for example to simulate a
mis-surveyed reference). `SimWorld` creates an emitter for every config Carrier that
has `location` and `emitter` and that no scenario emitter names.
`Scenario.validate_against(config)` raises on unknown ids, on an emitter id that
collides with such an automatic carrier emitter, and on 8-PSK (`core.signals` makes
BPSK and QPSK only; the field allows 8), naming the field. `EmitterSpec` rejects
`t_stop <= t_start`. Ephemeris truth errors with a non-zero `ric_vel_mps` need `t_ref`
or a `SimWorld` built with `epoch_ns`. Also `Scenario.emitter(id)`,
`load_scenario(path)`, `loads_scenario(text)`, `dump_scenario(scenario)` and
`ScenarioError` (a `ValueError`).

Shipped scenarios (`config/scenarios/`, for `pacific-uhf.yaml`; the server lists them
as presets by file stem; each `description` says what the operator should see, with
numbers measured through the server's chain as re-implemented in
`tests/test_sim_verify.py`):

```
pacific-demo      noise jammer 10 kHz at 301.005 MHz (shared-1), 20 dBW omni, 20 N 132 E; small truth errors; references on
clock-fault       pacific-demo plus the Naha site clock 10 us late
ephemeris-error   the same jammer at 17 N 141 E while muos-pac is 8 km ahead and uhf-172e 6 km behind in-track
                  (the solver assumes 1 km)
two-jammers       pacific-demo's jammer plus an 8 kHz, 18 dBW jammer at 301.130 MHz (shared-2), 30 N 135 E
uplink-cw         CW jammer, 5 dBW at 301.130 MHz, 15 N 130 E: FDOA only
uplink-swept      sawtooth chirp, 10 kHz every 40 ms around 301.005 MHz, 21 N 127 E: every correlation ambiguous
local-interferer  terrestrial noise, -40 dBW at 260.005 MHz (a MUOS shared-1 downlink), 4 km from Busan: local
quiet             only the reference carriers: no event
```

`SimWorld(config, scenario, *, epoch_ns=None)` (`epoch_ns`: reference time of ephemeris
errors that grow without a `t_ref`; the sim driver passes the backplane document's
`effective_ns`, the Monte Carlo driver its epoch):

- `render_iq(channel_id, t_start_ns, n, fs, fc) -> complex64[n]` implements MATH.md
  section 4 exactly (paths, visibility, channel filter, in-band slices, relayed noise,
  thermal noise keyed so repeat calls agree). Delay and phase come from a polynomial
  fit of the exact light-time functions over the window, fit error checked (< 1e-4
  cycle of carrier phase) and the window subdivided when needed. The channel filter's
  `W` is `Waveform.bandwidth_hz`; a decimated path is rendered with
  `decimate_margin(m)` extra samples at each end, which are dropped; direct paths
  point the emitter with `linkbudget.emitter_boresight`.
- `render_psd(channel_id, t_start_ns, fc, span_hz, nfft, navg, *, expected=False) ->
  float32[nfft]`: the expected Welch PSD without synthesising IQ. Linear power in bin
  k = 1 (thermal, 0 dB) + sum over transponders of `N0_relay / N0_site` on their
  passband bins + sum over paths of `(C_rx / (N0_site * span_hz)) * nfft * S_p[k]`,
  where `S_p[k] = waveform.spectral_shape(f_k - f_bb, resolution_hz=bin_hz) * bin_hz`
  (sum over k = 1 when the band is inside the span: flat over the bandwidth for noise
  and in-band slices, RRC squared for psk, the full Hann-window kernel of
  `welch_psd_db`, main lobe and sidelobes, for cw). A chirp uses instead the exact
  Fourier line spectrum of the sawtooth (Fresnel integrals, lines `1/P` apart, about
  1 % of the power beyond `+/- W/2`), each line spread over the bins by the Hann
  kernel and, for a path rendered directly at `fs`, lines beyond `+/- fs/2` folded in
  as `render_iq` aliases them (a flat band read up to 9 dB low just beyond the sweep
  edges). Decimated paths and slices are multiplied by the decimation filter's
  `|H(f)|^2`. Each path is scaled by the fraction of the snippet the emitter radiates
  (`gate_fraction`, on/off times); then every bin is multiplied by a
  `Gamma(shape=navg, scale=1/navg)` draw keyed by (scenario seed, channel, centre,
  `t_start_ns`), left out with `expected=True`. Measured against
  `welch_psd_db(render_iq(...))` on every window of every scenario: occupied bins within
  0.33 dB, empty bins within 0.19 dB (the test requires 1 dB). The per-realisation
  ripple of a chirp against the Welch segments (about +/- 1.5 dB) is not modelled.
- Budgets, Doppler and power robbing are evaluated on a node grid aligned to absolute
  time, 10 s apart when every satellite stays above 20 000 km and 1 s otherwise, and
  interpolated linearly between nodes (power and received frequency). Robbing uses the
  emitters active at the node instant, a gated emitter at its on-power; an emitter that
  switches on between nodes robs from the next node.
- `truth_measurement(emitter_id, a: PathSpec, b: PathSpec, t_ns) -> (tdoa, fdoa)` with
  all truth errors applied (the TDOA fixed point iterated to convergence; computed
  whatever the visibility); `path_budget(emitter_id, sat_id, transponder_id,
  channel_id, t_s) -> PathBudget | None` (also None when the emitter is not radiating);
  `truth_orbit(sat_id)`, `truth_orbits()`; `emitters`, `emitter(id)`,
  `emitters_at(t_ns)` (`ResolvedEmitter` records, `as_dict()` for `/sim/truth`);
  `truth_summary(t_ns)` (JSON-ready truth for display); `channel_paths(channel_id,
  t_ns, fc, fs) -> [PathInfo]` (every path into a channel and how a capture treats it);
  `direct_power_ratio_dbhz(emitter_id, channel_id, t_s)` (C/N0 of a direct path, None
  outside the radio horizon); `site_clock_offset_s`, `site_osc_frac`, `sat_osc_frac`,
  `true_translation_hz`.
- A satellite whose truth orbit cannot be evaluated at an instant (an ephemeris table
  that has ended or not begun) has no paths then: an emitter aimed at it radiates no
  uplink, a channel tracking it renders receiver noise only, a capture reaching past
  the end of its table drops its paths, and `truth_summary` leaves it out; the other
  satellites are unaffected. Each case is logged once (`simulated <what> cannot be
  placed at t = ... (...); its paths are left out`).
- `rebuild(config=None, scenario=None, *, epoch_ns=None) -> SimWorld` carries
  unchanged waveforms and their caches over (1 to 2 ms against 36 to 43 ms for a new
  world). Methods are thread-safe. Grid waveforms cache at most 8 MiB each.
- Measured cost on the shared 4-core machine: `render_iq` of 20 000 samples at 20 kHz,
  68 to 76 ms median; 262 144 samples at 250 kHz, 0.5 to 1.5 s; a Ku-band slice capture
  (262 144 samples, six slice paths), 4 to 6 s; `render_psd` of 1024 bins, 0.2 to
  0.4 ms median, about 6 ms once per 10 s per channel when a new budget node is built.

`montecarlo.py`: `run_trials(config, scenario, emitter_id, n_trials, *, pairs=None,
seed=0, draw_errors=False, use_calibration=True, capture_s=None, fs=None, t_ns=None,
progress=None, reference_carrier_id=None) -> dict` drives render_iq -> caf_estimate ->
solve directly (no network) and follows tasking (section 7) where it matters:

- Primary channel by link-budget C/N0 (standing in for the detector's `snr_db`);
  partners by `downlink_quality_db` with the 3 dB same-site preference; or the caller's
  `pairs`, whose ends may be `channel@sat_id` for a channel watching several satellites
  that relay the frequency (`core.accuracy.pair_satellites`; a channel paired with
  itself is refused). Captures and windows from `core.accuracy.capture_plan` and
  `pair_search`, self-peak exclusions and mirror windows as in tasking.
- The band filter is `max(occupied band, MIN_SIGNAL_BW_HZ = 10 Hz)` wide (plus the
  Doppler reach), as in tasking and the planning map, so a CW is band-limited too and
  a reference carrier a few kHz away does not correlate with it.
- Rows carry capture ids (`<channel>|<render_ns>|<n>|<fs>|<fc>`) and the estimator's
  noise shares, so the solver models the noise that pairs sharing a capture have in
  common (MATH.md section 9), as tasking does.
- A kind without a valid calibration, and every mirror window, is first widened by
  `ephemeris_allowance(uncertainty, sat_a, sat_b, f_hz) -> (delay_s, doppler_hz)`
  (public): `6 (|sigma_pos,a| + |sigma_pos,b|) / c` and `6 f (|sigma_vel,a| +
  |sigma_vel,b|) / c` with the RIC one-sigma vectors, the same allowance as tasking.
- Calibration: the first reference carrier in configuration order relayed by both of
  the pair's transponders (or `reference_carrier_id`; tasking prefers the one nearest
  the current fix, which this driver does not), captured through the same channels at
  the same start time as the targets (tasking uses a later time slot), turned into
  corrections with `solver.calibration_correction`.
- CAF results that are undetected, ambiguous, coupled or mirror-ambiguous are recorded
  with their reason (`REASON_MIRROR_AMBIGUOUS = "mirror ambiguous"`) and not solved; a
  mirror-ambiguous reference gives no calibration (as tasking).
- There is no 2.2 x Doppler sample-rate rule, no window-driven capture lengthening and no
  repeat-detection gate; none of these changes a GEO result.
- Each trial is a pure function of `(seed, trial index)`. With `draw_errors` it replaces
  the scenario's ephemeris, oscillator and clock errors by a draw from the configured
  one-sigma uncertainties, velocity errors referred to the measurement epoch, plus a
  world-wide ionosphere `|N(0, iono_vtec_tecu)|` and zenith delay `|N(0,
  tropo_zenith_m)|` (`draw_truth_errors` returns them as `iono` and `tropo`; a
  scenario that enables its own ionosphere or troposphere keeps it), plus a per-pair
  TDOA bias, which the simulator has no model for, applied by rendering channel B that
  much later than its label (its reference capture carries the same bias, so it cancels
  under calibration as the solver assumes). The media are drawn after the site errors,
  so per-seed trials from before they were added are not reproduced.
- The base world's `epoch_ns` and the capture start are `t_ns`, else `DEFAULT_T_NS`
  (2026-01-01T00:00:00Z) or the emitter's `t_start` when later.
- Returns a JSON-ready dict marked `simulated: True`: the plan (`primary_channel`,
  `pairs`, `skipped`), `truth`, per-trial results (CAF per pair, `miss_m`, `error_en_m`,
  `nees_total`, `nees_noise`, `inside_95_total`, `inside_95_noise`, `cep50_m`,
  `semi_major_m`, `chi2`, `dominant_error`, `ambiguity_resolved_by`, `n_ambiguities`, or
  a `reason`) and `summary` (`n_solved`, `failures`, `median_miss_m`, `p95_miss_m`,
  `mean_error_en_m` with its standard error, mean NEES and 95 % containment with
  `P_total` and `P_noise`, predicted and empirical CEP50, per-pair rejection counts).
  `format_summary(result)` prints a few lines; `plan_event(...)` is the pair choice
  alone. The truth position is the emitter's latitude and longitude while the solver
  works at `solver.height_m`, so an emitter antenna 10 m up carries a small geometric
  bias that is part of the result. Cost: about 1 s per trial at 1 s captures with
  references.

## 5. Core API (signatures other modules rely on)

Shapes: `x_e` may have leading axes `[..., 3]`; time arguments may be a float or
`ndarray[M]`; results broadcast to `[..., M]` (`[...]` for scalar time).
`orbits` is `dict[sat_id, Orbit]`, `sites_xyz` is `dict[site_id, ndarray[3]]`.

```python
# timebase.py
now_ns() -> int;  ns_from_iso(s: str) -> int;  iso_from_ns(t_ns: int) -> str;  ns_from_datetime(value) -> int
    # ns_from_iso: no zone designator means UTC; ":60" and more than 9 fractional digits raise ValueError
sec(t_ns: int) -> float                                 # correctly rounded t_ns / 10**9
gmst_rad(t_s, dut1_s=0.0);  wrapped_phase(rate_rad_s, dt_s)   # (rate * dt) mod 2 pi from an exact product
MAX_UNIX_S = 1e11                                       # a larger |t_s| is t_ns passed as seconds: ValueError

# geodesy.py   (degrees in, metres out)
wrap_lon_deg(lon_deg)                                   # to [-180, 180)
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
TableOrbit.from_samples(samples)                         # config-style [{t, r_m, v_mps}]
    # these set supports_split_time and take state_ecef(t_s, dt_s=0.0) and position_ecef(t_s, dt_s=0.0):
    # the state at t_s + dt_s without rounding the sum
orbit_position(orbit, t_s, dt_s=0.0) -> ndarray[..., 3];  orbit_state(orbit, t_s, dt_s=0.0) -> (r, v)
    # any Orbit; an object without supports_split_time is evaluated at float64(t_s + dt_s)
orbit_from_config(orbit_model, dut1_s=0.0) -> Orbit     # reads attributes; accepts the pydantic models or any object with the same fields
subsatellite_point(orbit, t_s) -> (lat_deg, lon_deg, alt_m)
tle_checksum(line) -> int

# geometry.py
    # every delay function takes an optional trailing dt_s: the result for time + dt_s exactly (a capture start plus n / fs)
uplink_delay(x_e, orbit, t_sat_s, dt_s=0.0) -> tau_u
downlink_delay(orbit, x_g, t_rx_s, dt_s=0.0) -> tau_d
@dataclass BentPipe: D, tau_d, tau_u, t_sat_s           # D, tau_u: [..., M]; tau_d, t_sat_s: [M] (t_sat_s is the rounded float)
bent_pipe(x_e, orbit, x_g, t_rx_s, tau_x=0.0, dt_s=0.0) -> BentPipe
bent_pipe_rates(x_e, orbit, x_g, t_rx_s, tau_x=0.0, dt_s=0.0) -> (D_dot, tau_d_dot, tau_u_dot)   # tau_u_dot = d tau_u / d t_sat
baseband_frequency(f_e, translation, f_tune, tau_d_dot, tau_u_dot, eps_g=0.0) -> f_bb
@dataclass(frozen=True) PathSpec: sat_id, site_id, f_e_hz, translation_hz, tau_x_s=0.0
                        # property f_tune_hz = f_e_hz + translation_hz
predict_tdoa(x_e, a: PathSpec, b: PathSpec, t_rx_s, orbits, sites_xyz) -> ndarray[..., M]     # MATH section 5, t_rx_s = t_mid
predict_fdoa(x_e, a: PathSpec, b: PathSpec, t_rx_s, orbits, sites_xyz) -> ndarray[..., M]
predict_pair(x_e, a: PathSpec, b: PathSpec, t_rx_s, orbits, sites_xyz) -> (tdoa, fdoa)        # both from one evaluation
    # the three predictors raise ValueError when the two path delays differ by more than 1 s
STENCIL_STEP_S = 0.5                                    # h of the rate stencil, MATH section 3
elevation_deg(x, orbit, t_s) -> ndarray                  # elevation of the satellite seen from ground point(s) x
footprint(orbit, t_s, min_elevation_deg=0.0, n=181) -> list[(lat_deg, lon_deg)]
    # ring from due north of the sub-satellite point running west (counter-clockwise), longitudes continuous around the
    # sub-satellite longitude; closed (last vertex = first) except that a ring containing a pole ends 360 degrees from its start
measurement_windows(a, b, t_rx_s, orbits, sites_xyz, region, min_elevation_deg=0.0, height_m=0.0, step_deg=2.0)
    -> ((tdoa_min, tdoa_max), (fdoa_min, fdoa_max)) | None
    # a bound on the predictions over the continuous admissible area (MATH section 6), elevation >= min_elevation_deg;
    # step_deg is the largest grid step (reduced for low satellites and small regions); None when no grid point and
    # no mask-contour sample is admissible
search_windows(geo_windows, a, b, caf_cfg, uncertainty, correction=None) -> (delay_window, doppler_window)   # MATH section 6 widening
    # correction: None, (tdoa_corr | None, fdoa_corr | None) or {"tdoa": ..., "fdoa": ...}; each kind shifted on its own
self_peak(sat_id, translation_hz, f_e_hz, a: PathSpec, b: PathSpec, t_rx_s, orbits, sites_xyz) -> (tdoa, fdoa)
    # satellite sat_id heard by both channels: exact CAF location including the tuning offsets of a and b; TDOA solved to
    # convergence; omits + f_e tau_u_dot (tau_d,a_dot - tau_d,b_dot), which needs the emitter position (below 1e-6 Hz on
    # GEO, up to a few tenths of a hertz in low orbit)

# antennas.py
gain_dbi(antenna_model, off_axis_deg, freq_hz) -> ndarray
peak_gain_dbi(antenna_model, freq_hz);  hpbw_deg(antenna_model, freq_hz)
electrically_small(antenna_model, freq_hz) -> bool      # parabolic with D < 3 wavelengths
# parabolic: G0 = eta (pi D / lambda)^2; main lobe -12 (theta/HPBW)^2 dB with HPBW = 70 lambda / D degrees,
# floored by the side-lobe envelope min(G0, 32 - 25 log10(theta)) dBi for 1..48 deg and -10 dBi beyond. Inside 1 deg the
# envelope is held at its 1 deg value (without that a large dish has a 3 dB hole just inside 1 deg).
# Electrically small (D < 3 lambda): HPBW = min(70 lambda / D, 120), floor G0 - 15 dB. Never below -10 dBi (G0 included).
# hpbw_deg: 360 for isotropic and for a table that never falls 3 dB below its boresight value.

# linkbudget.py   (boresight arguments are unit ECEF vectors; None = "omni")
fspl_db(dist_m, freq_hz)
transponder_noise_eirp_dbw_hz(transponder) -> float     # EIRP_n0 of MATH section 4, before power robbing
uplink_budget(eirp_dbw, antenna, boresight, x_e, sat_r, f_e_hz, transponder)
    -> UplinkBudget(eirp_toward_sat_dbw, pfd_dbw_m2, cn0_up_dbhz, elevation_deg)      # broadcast over x_e
transponder_outputs(pfd_dbw_m2: ndarray[K], transponder)
    -> TransponderOutputs(eirp_down_dbw: ndarray[K], relayed_n0_eirp_dbw_hz: float, backoff_db: float)
    # backoff_db = EIRP_sat - (carriers + noise over bandwidth_hz, unclipped), dB: positive is back-off; negative means
    # saturated, every carrier and the noise reduced by -backoff_db (also .compression_db >= 0). pfd [..., K] gives arrays.
downlink_budget(eirp_down_dbw, relayed_n0_eirp_dbw_hz, f_d_hz, sat_r, site_xyz, channel, boresight)
    -> DownlinkBudget(c_rx_dbw, relayed_n0_dbw_hz, site_n0_dbw_hz, elevation_deg, off_axis_deg)
    # properties n0_total_dbw_hz = 10 log10(N0_site + N0_relay), relay_rise_db = 10 log10(1 + N0_relay / N0_site)
direct_budget(eirp_dbw, antenna, boresight, x_e, h_e_m, site_xyz, h_g_m, channel, channel_boresight, f_hz)
    -> DirectBudget(c_rx_dbw, in_horizon: bool)     # c_rx_dbw = -inf outside the radio horizon; ValueError within one wavelength
emitter_boresight(emitter, x_e, orbits, t_s) -> unit ndarray[..., 3] | None
    # None for "omni"; {satellite_id}: toward its geometric position at t_s; {az_deg, el_deg}: local, azimuth
    # clockwise from north
@dataclass PathBudget: uplink, transponder_eirp_dbw, downlink, cn0_total_dbhz;  snr_db(bandwidth_hz)
path_budget(config, emitter: EmitterModel, f_e_hz, x_e, sat_id, transponder_id, channel_id, t_s, other_pfd_dbw_m2=()) -> PathBudget | None
    # reads emitter.eirp_dbw, .antenna, .pointing only; t_s scalar; None when f_e_hz is outside the transponder's uplink
    # passband or a leg is below the horizon; for a batch of x_e, arrays with -inf where the emitter cannot see the satellite
downlink_quality_db(config, channel_id, sat_id, transponder_id, t_s) -> float | None      # MATH section 7; None below the horizon
relay_rise_db(config, channel_id, sat_id, transponder_id, t_s) -> float | None    # R_k of MATH section 7; None below the horizon

# iono.py
slant_factor(el_deg);  group_delay_s(vtec_tecu, el_deg, freq_hz)     # el_deg outside [0, 90] raises ValueError

# media.py   (ionosphere and troposphere consider partials, MATH sections 3 and 9)
MEDIA = ("ionosphere", "troposphere")
tropo_mapping(el_deg)                                   # Black-Eisner 1.001 / sqrt(0.002001 + sin^2 el)
tropo_delay_s(zenith_m, el_deg)
leg_partials(ground, orbit, t_s, freq_hz) -> {medium: (delay_s, path_frequency_hz)}   # per TECU / per metre of zenith delay
row_media_partials(kind, a, b, t_s, emitter, orbits, sites_xyz, emitter_key="emitter", *, uplinks=True, downlinks=True)
    -> {medium: {ground_end_key: partial}}      # per ground end of the row (emitter, each site, a reference position)
    # uplink legs at the emitter's frequency and elevation, downlink legs at the site's; FDOA convention: the
    # ionosphere adds +f d(delay)/dt and the troposphere -f d(delay)/dt to a path's frequency

# crlb.py
effective_snr(g1, g2);  coherence(g1, g2);  post_correlation_snr(noise_bw_hz, T_s, g1, g2) -> rho
stein_sigmas(noise_bw_hz, rms_bw_hz, T_s, g1, g2, *, rho_tf=0.0) -> (sigma_tdoa_s, sigma_fdoa_hz)
    # with the q term when rms_bw < B/sqrt(12); rms_bw = 0 gives inf; rho_tf: the coupled variances of MATH section 8
sigmas_from_coherence(C, noise_bw_hz, rms_bw_hz, T_s, noise_rms_bw_hz=None, *, rho_tf=0.0, signal_noise_share=None)
    -> (sigma_tdoa_s, sigma_fdoa_hz)
    # noise_rms_bw_hz multiplies sigma_tdoa by F = noise_rms_bw_hz / rms_bw_hz (MATH section 6 item 4)

# spectrum.py
welch_psd_db(iq: complex[nfft*navg], fs, nfft, navg) -> float32[nfft]     # MATH section 7 normalisation; fft-shifted

# signals.py
class Waveform: evaluate(base_ns: int, t_rel: ndarray) -> complex128;  bandwidth_hz;  rms_bandwidth_hz;
                spectral_shape(freqs_hz, resolution_hz=None) -> ndarray;  gate(base_ns, t_rel) -> bool ndarray;
                gate_fraction(base_ns: int, duration_s) -> float;  duty;  gate_period_s
    # spectral_shape: density (1/Hz) of the ungated envelope; with a resolution (given, or the spacing of a uniform 1-D
    # freqs_hz) each value is averaged over one bin, so sum * bin width is the in-span power fraction; cw returns the
    # full Hann-window kernel of welch_psd_db and needs a resolution. bandwidth_hz: 0 for cw, symbol_rate_hz (1 + rolloff)
    # for psk. gate_fraction: exact on-fraction of [base_ns, base_ns + duration_s) (MATH section 4)
CwWaveform, ChirpWaveform, NoiseWaveform, PskWaveform (symbols(n_first, count): the symbol centred on n / symbol_rate_hz)
waveform_from_spec(spec, key_label: str) -> Waveform     # spec.seed is not read: it is part of key_label
key128(label: str) -> int

# channel.py
render_path(waveform, base_ns, n, fs, delay_fn, phase_fn, amplitude) -> complex128[n]
    # delay_fn(t_rel: float64[n]) -> seconds, phase_fn(t_rel) -> cycles; t_rel = k/fs seconds of labelled time after base_ns
decimate(x, m) -> ndarray            # linear-phase FIR of MATH section 4, group delay removed exactly; ceil(len(x) / m)
                                     # samples, x taken as zero outside the array
decimate_margin(m) -> int            # output samples at each end carrying that edge transient (26; 0 for m = 1)
decimation_fir(m) -> ndarray         # its 52 m + 1 taps
thermal_noise(n, key_label) -> complex128[n]      # E|x|^2 = 1

# caf.py
@dataclass CafResult: tdoa_s|None, fdoa_hz|None, sigma_tdoa_s|None, sigma_fdoa_hz|None, rho_tf, snr_db, coherence, detected: bool,
                      mirror_ambiguous: bool, candidates: list[(tdoa_s, fdoa_hz, snr_db)], rms_bandwidth_hz|None,
                      surface: float32[<=65, <=65] dB, delay_axis_s, doppler_axis_hz,
                      delay_cut: (axis_s, dB), doppler_cut: (axis_hz, dB), reason: str|None,
                      noise_bandwidth_hz: float (B used), integration_s: float (T: samples in the sum / fs),
                      snr_a_db|None, snr_b_db|None, snr_measured: bool, noise_share_tdoa: (a, b), noise_share_fdoa: (a, b),
                      leak_excess: float
    # sigmas and rms_bandwidth_hz are None when not detected or not finite, never inf or NaN.
    # snr_a_db / snr_b_db: per-channel SNR, the noise floor read from the lower quartile of the Welch spectrum outside
    # the filter band (snr_measured); equal SNRs from the coherence when there is no room outside the band.
    # noise_share_*: each channel's share (1/g)/total of the reported variance (solver shared-capture columns).
    # leak_excess: when excluded self-peaks coincide (one site hearing two satellites in both channels), their sidelobes
    # add in amplitude; min(2 sqrt(C0 Cm), Cs - 2 sqrt(C0 Cm)) is added to (1 - C)/C before the sigmas (MATH section 6)
REASON_BELOW_THRESHOLD = "below threshold";  REASON_AMBIGUOUS = "ambiguous";  REASON_COUPLED = "delay-doppler coupled"
REASON_NARROWBAND = "narrowband" (FDOA valid, TDOA None);  REASON_OUTSIDE = "peak outside window"
caf_estimate(ya: complex[n], yb: complex[n], fs, *, delay_window: tuple[float, float], doppler_window: tuple[float, float],
             signal_bw_hz: float | None = None, t0_offset_s: float = 0.0, exclude: Sequence[tuple[float, float]] = (),
             mirror_window: tuple[tuple[float, float], tuple[float, float]] | None = None,
             min_snr_db=13.0, max_tdoa_sigma_s=5e-4) -> CafResult
    # ValueError (naming the argument) when a delay window plus its guard reaches beyond half the capture, or a Doppler
    # window leaves +/- fs/2 or spans more than fs/2; ya and yb may differ in length

# detect.py
@dataclass Detection: track_id: int, f_lo_hz, f_hi_hz (downlink), power_db, snr_db, cn0_dbhz,
                      first_seen_ns, last_seen_ns, authorized_carrier_id|None, anomaly: bool
class SignalTracker:
    __init__(center_hz, span_hz, nfft, frame_rate_hz, cfg,
             passbands: list[(f_lo_hz, f_hi_hz, relay_rise_db)], carriers: list[(carrier_id, f_lo_hz, f_hi_hz)])
        # nfft >= 16; ValueError when cfg.baseline_tau_s spans fewer than 10 frame periods
    update(psd_db: float32[nfft], t_ns: int) -> list[Detection]      # every confirmed, unreleased track
        # a track on an authorised carrier only while anomalous; ValueError for a non-increasing t_ns or non-finite bins
    baseline_db: float32[nfft]                                       # NaN before the first update
    effective_threshold_db: float                                    # threshold in force, MATH section 7
    floor_db: float                  # start-up floor F (a bin's median noise level without relayed noise); NaN before a frame
    floor_from_guard: bool;  learning: bool                          # learning: fewer than half the bins on their own history
    reset_time() -> None             # the next frame is accepted whatever its t_ns (a node clock step); baseline kept
FLOOR_LOWER_QUANTILES = (0.25, 0.10, 0.04, 0.015);  FLOOR_LEAST_BINS = 8;  FLOOR_SIGNIFICANCE = 4.0;  FLOOR_REFINE_STEPS = 3
    # F starts from the guard median (or the corrected 10th percentile without guard bins) and steps to a lower corrected
    # quantile when it is more than 4 standard errors lower (MATH section 7), so a signal on from the first frame is found
classify(iq, fs) -> Classification(label, confidence, features: dict)   # label in CLASS_LABELS; at least 1024 samples

# solver.py
@dataclass(frozen=True) CalRef: cal_id: str, x_ref: ndarray[3], a: PathSpec, b: PathSpec, t_ns: int, sigma: float,
                        capture_a: str = "", capture_b: str = "", noise_share_a: float = 0.0, noise_share_b: float = 0.0
@dataclass Measurement: kind ("tdoa"|"fdoa"), value, sigma (CAF noise of this measurement only), t_ns (t_mid of MATH section 5),
                        a: PathSpec, b: PathSpec, correction: float = 0.0, cal: CalRef | None = None, tag: str = "",
                        capture_a: str = "", capture_b: str = "", noise_share_a: float = 0.0, noise_share_b: float = 0.0
    # capture ids name the capture each channel's samples came from; shares are each channel's part of sigma^2
    # (>= 0, sum < 1, capture_a != capture_b when set; ValueError otherwise). A capture id named by two or more rows of
    # one kind becomes a shared CAF-noise consider column (side x sigma x sqrt(share)); those rows' own sigma becomes
    # sigma sqrt(1 - shared shares), never below 0.1 sigma. References sharing a capture are split the same way.
@dataclass Uncertainty: sat_pos_ric_m: dict[sat_id, ndarray[3]], sat_vel_ric_mps: dict[sat_id, ndarray[3]],
                        site_clock_s: dict[site_id, float], site_osc_frac: dict[site_id, float],
                        sat_osc_frac: dict[sat_id, float], tdoa_bias_s: float,
                        iono_vtec_tecu=0.0, iono_correlation=0.0, tropo_zenith_m=0.0, tropo_correlation=0.0
    # media are off when an Uncertainty is built directly; SystemConfig.resolved_uncertainty() copies the configured
    # values (30 TECU / 0.5, 2.4 m / 0.9). Negative sigmas or correlations outside [0, 1]: ValueError in the solver.
    # Media columns: one per ground end and medium scaled sigma sqrt(1 - rho), plus one common column sigma sqrt(rho);
    # calibrated rows subtract the reference's own partials. A calibrated row's reference velocity partial is referred
    # to the row's epoch: d h_ref / d vel - (t_k - t_ref) d h_ref / d pos (MATH section 9).
@dataclass Solution: lat_deg, lon_deg, height_m, cov_en_m2 (2x2 total), cov_noise_en_m2, cov_consider_en_m2,
                     ellipse: {semi_major_m, semi_minor_m, azimuth_deg, confidence}, cep50_m,
                     chi2, dof, reduced_chi2: float | None, residuals (whitened, input order),
                     ambiguities: list[Solution], ambiguity_resolved_by: "none"|"region"|"measurements"|"unresolved",
                     converged: bool, dominant_error: str, n_measurements: int
                     Solution.as_dict() -> JSON-ready dict (matrices as nested lists)
solve(measurements, orbits, sites_xyz, solver_cfg, region, uncertainty: Uncertainty | None = None,
      starts: Sequence[tuple[float, float]] | None = None) -> tuple[Solution | None, str | None]     # (solution, reason)
    # reason: "unobservable" | "no admissible region" | "no admissible solution" | "not converged" (MATH section 9)
calibration_correction(ref_measurement: Measurement, x_ref, orbits, sites_xyz, cal_id) -> tuple[float, CalRef]
    # reversed pair: use -c with the same CalRef; ValueError for a reference row with a correction or cal, or on other satellites
position_covariance(measurements, lat_deg, lon_deg, orbits, sites_xyz, height_m=0.0, uncertainty=None, confidence=0.95)
    -> {cov_en_m2, cov_noise_en_m2, cov_consider_en_m2, ellipse, cep50_m, dominant_error, budget_m2} | None
    # J, S and observability rule of solve at a given position, without solving (row values unused); None when unobservable
error_ellipse(P_2x2, confidence) -> dict;  cep50(P_2x2) -> float
above_mask(lat_deg, lon_deg, x, r_sat, min_elevation_deg) -> bool array
ERROR_SOURCES = ("noise", "ephemeris_position", "ephemeris_velocity", "clock", "oscillator", "tdoa_bias", "reference",
                 "ionosphere", "troposphere")       # values of dominant_error and of budget_m2 keys
    # cov_noise_en_m2 includes the shared capture noise: K (D^2 + G_noise G_noise^T) K^T

# lop.py
lop_geojson(measurement, orbits, sites_xyz, region, height_m=0.0, min_elevation_deg=0.0, step_deg=0.25, k_sigma=2.0) -> dict   # FeatureCollection
    # 0 to 2 features, properties.role "line" (MultiLineString) or "band" (MultiPolygon); MATH section 11

# accuracy.py
accuracy_map(config, pairs: list[tuple[str, str]] (ends A, B: channel ids, or "channel_id@sat_id"), emitter: EmitterModel,
             f_e_hz, t_ns, region, step_deg, kinds=("tdoa", "fdoa"), reference_carrier_id: str | None = None,
             integration_s: float | None = None) -> dict
    # keys lat_deg[n_lat], lon_deg[n_lon] (unwrapped), semi_major_95_m[n_lat][n_lon], cep50_m[n_lat][n_lon],
    # dominant[n_lat][n_lon]; null where not admissible or unobservable. Also semi_minor_95_m, azimuth_deg, n_rows,
    # reason ("not admissible" | "unobservable" | null) grids, confidence (always 0.95, whatever solver.confidence),
    # t_ns, f_e_hz, step_deg, kinds, reference_carrier_id and pairs (per pair: channels, satellites, transponders,
    # sample_rate_hz, n_samples, noise_bandwidth_hz, calibrated kinds, the reference's predicted sigmas and SNR, and a
    # reason when the pair contributes nothing anywhere; a_channel / b_channel keep the text as listed). The API returns
    # only the AccuracyGrid keys (PROTOCOL.md B). Each channel's noise counts every satellite heard in the capture band
    # (its relayed noise and the co-channel copies of the carrier it relays); CW and narrow carriers are correlated over
    # max(width, MIN_SIGNAL_BW_HZ) plus the Doppler reach, as tasking does; same-site co-channel pairs carry the
    # coincident self-peak term (copies taken in phase); media columns and shared-capture noise as in the solver
    # (MATH section 10). The reference is the one named; the map does not choose one.
# Shared with the Monte Carlo driver, which plans captures as the map does. Tasking implements the same rules
# itself (section 7) and adds the ones listed there (ephemeris allowance, Doppler sample rate, capture lengthening).
MIN_SIGNAL_BW_HZ = 10.0                                  # narrowest band filter of tasking, the map and the Monte Carlo
split_channel_ref(text) -> (channel_id, sat_id | None)   # "channel@sat" -> ("channel", "sat")
channel_satellite(config, channel_id, f_e_hz, name="channel_id", *, exclude=None, sat_id=None) -> (sat_id, transponder) | None
    # the satellite relaying f_e into the channel; accepts "channel@sat"; checks that sat is watched; exclude drops
    # one satellite when several relay the carrier; ValueError naming the "channel@<sat_id>" form when ambiguous
pair_satellites(config, end_a, end_b, f_e_hz, name="pair") -> ((sat_id, transponder) | None, (sat_id, transponder) | None)
    # both ends together: an ambiguous end takes the satellite the other end does not use
capture_plan(config, channel_ids, f_lo_hz, f_hi_hz, *, duration_s=None, sample_rate_hz=None)
    -> CapturePlan(sample_rate_hz, n_samples, f_e_nom_hz, occupied_bw_hz)   # .signal_bw_hz is None for zero width (no filter)
pair_search(config, a, b, t_mid_s, sample_rate_hz, occupied_bw_hz, *, point_xyz=None) -> PairSearch | None
PairSearch.windows(caf_cfg, uncertainty, correction=None) -> (delay_window, doppler_window, mirror_window | None)
    # exclusions in PairSearch.exclude; point_xyz (a reference carrier) gives point windows at its prediction
correlation_bandwidth_hz(sample_rate_hz, signal_bw_hz, doppler_reach_hz, n_samples) -> (B, n_taps)   # caf_estimate's filter
integration_time_s(n_samples, n_taps, sample_rate_hz, tdoa_s, t0_offset_s=0.0)        # T at a predicted TDOA
covariance_batch(jacobian, sigma, active, gain, categories) -> dict, cep50_batch(cov)  # the solver's covariance, vectorised
    # columns whose category is "noise" (shared capture noise) go into the noise matrix and the noise budget
    # B equals CafResult.noise_bandwidth_hz and T CafResult.integration_s to 1e-12 (tested); accuracy.py imports the
    # private caf._band_filter for that. Default Pacific region at 2 degrees, two pairs: 0.25 s, 0.42 s with a reference.
```

`server/api/rest.py` also exports `accuracy_config(cfg, request)`: the configuration
with `AccuracyRequest.uncertainty` in place of `processing.uncertainty` (satellite
`ephemeris_sigma` and site clocks keep theirs); `accuracy_problems(cfg, request)` (the
422 checks, also used by the CLI: unknown or repeated channels, a pair named twice,
more than `ACCURACY_MAX_PAIRS = 128` pairs, two grid rows needing more than
`ACCURACY_BAND_BYTES = 256 MiB` at `ACCURACY_BYTES_PER_CELL_PAIR2 = 160` bytes per cell
per pairs squared, a reference carrier that is not one); `accuracy_band_bytes(n_pairs,
cells)` and `accuracy_band_cells(n_pairs, n_lon)` (bands of at most
`ACCURACY_BAND_CELLS = 256` cells, fewer when the memory estimate demands, never under
two grid rows); `MAX_BODY_BYTES = 4 MiB`; `resolve_time_ns` (422 outside 1970..2262);
`token_warnings(settings, site_ids)`; and `compute_link_budget`, which leaves out (and
logs) satellites whose orbit cannot be evaluated at `t`. `accuracy_problems` checks pair
ends against channel ids as written, so the `channel@sat` form is refused there.

## 6. Node agent (`node/`)

One process per site. `drivers/base.py`:

```python
@dataclass Capture: iq: complex64[n], t_first_ns: int, fs, fc, overflow: bool, clock_locked: bool
@dataclass Capabilities: min_hz, max_hz, max_sample_rate_hz, lookback_s, concurrent_reads
@dataclass DriverContext: site_id, server_http_url, token, simulation: dict | None,
                          clock: ClockSource | None = None, workers: WorkerPool | None = None   # the node's clock and 2-thread pool

class SdrDriver(Protocol):
    capabilities: Capabilities
    async def open(self, channel: ChannelAssignment, ctx: DriverContext) -> None
    async def read(self, t_start_ns: int, n: int, fs: float, fc: float) -> Capture
        # t_start_ns may be slightly in the future (driver waits) or in the recent past
        # (lookback); raises CaptureError(code, message) when it cannot serve the interval.
    async def psd(self, t_start_ns: int, fc: float, span_hz: float, nfft: int, navg: int) -> ndarray | None
        # float32[nfft] in welch_psd_db units; None when the driver has no fast path
    async def close(self) -> None
    # optional hooks: async on_assign(channel, ctx) (an unchanged channel after an assign, before the ack) and
    # async on_sim_update(version, effective_ns) (a sim_update arrived)
```

Monitor loop: call `driver.psd(...)`; if it returns `None`,
`read(t_start_ns, nfft * navg, span_hz, fc)` and apply `welch_psd_db`. Frame start
times are the node clock at each tick; frames carry what the driver measured.

`get_driver(name, **options)` builds a driver and refuses unknown options; one driver
is built per assigned channel, with `driver_options` overlaid by that channel's entry of
`channel_driver_options` (`NodeAgent(channel_driver_options={channel_id: {...}})`;
`petal-locate node --channel-option CHANNEL:KEY=VALUE` or a `--node-config` file), so a
site with one radio per channel gives each its device arguments. `driver_factory` still
replaces the whole rule.

Drivers:

- `noise`: unit noise; protocol and plumbing tests.
- `sim`: one `SimWorldSource` per node (a registry keyed by event loop, server origin,
  backplane path, site and token), fetched from the backplane with httpx (decoding and
  world building in the node's worker pool), refetched on `open`, `on_assign` and
  `on_sim_update` (concurrent requests share one fetch; failures retried with backoff).
  A capture is served whole from the world in force at its first sample; at most two
  worlds are kept, the first one fetched also serves earlier times, an interval only a
  dropped world could serve gets `too_late`, and a version announced but superseded
  before this node fetched it gets `driver`. A read waits for an announced, not yet
  fetched version at most `world_wait_s`. `read` returns exactly `render_iq` and `psd`
  exactly `render_psd`, each only after the node clock has passed the end of its
  interval; lookback 30 s; `concurrent_reads` 8; capabilities cover every downlink of
  every watched satellite and every monitor window. Captures report `clock_locked`;
  `petal-locate node --driver sim` runs with `sim_clock(offset_ns)` (source `sim`,
  locked) unless `--clock` is given, so demo nodes report `sim`. A held `sim_update`
  version that arrives again (the hub re-sends after `assign_ack`) is ignored.
- `sigmf`: replays a `cf32_le` SigMF recording with its timestamps
  (`core:datetime` of the first capture segment, read to the nanosecond, or the
  `t0_ns` option); later segments are taken to follow without a gap. A request for the
  recording's own rate and centre returns its samples bit for bit; others inside the
  recorded band are mixed and resampled (`scipy.signal.resample_poly`, about -6 dB at
  the new band edge), others fail `out_of_range`. Without `loop` an interval outside
  the recording fails `too_late`; with `loop=true` the recording repeats (the join is
  an unhidden discontinuity). Options `path`, `loop`, `t0_ns`, `realtime`,
  `clock_locked`, `concurrent_reads`.
- `soapy`: SoapySDR with hardware timestamps and a lookback ring (`buffer_s`, 4 s);
  one stream per channel tuned to cover its monitor windows, digital down-conversion
  per read, a new segment at every overflow or timestamp jump so that no capture spans
  lost samples. `time_sync` is `pps` (latch UTC on the next PPS edge; needs a PPS input
  aligned to UTC), `host` (set the device clock from the node clock; milliseconds) or
  `device` (leave it; already UTC); `lock_sensor` names the device sensor that says the
  reference is locked. Options `device`, `rf_channel`, `center_hz`, `sample_rate_hz`,
  `bandwidth_hz`, `gain_db`, `antenna`, `clock_source`, `time_source`, `time_sync`,
  `lock_sensor`, `buffer_s`, `concurrent_reads`. **Untested on hardware and against the
  real SoapySDR library** (tested against a software stand-in only); the bindings (the
  system package `python3-soapysdr` or SoapySDR built from source; not on PyPI, and no
  pip extra provides them) are imported when a channel opens.

Clocks (`node/clock.py`): `SystemClock(source="ntp", locked=True, offset_ns=0)` by
default, which does not ask the operating system whether NTP is synchronised
(`offset_ns`, simulation only, is added to the host clock: `petal-locate demo --start`
gives every simulated node the server's offset; read-only `offset_ns` property, also on
`DisciplinedClock`); `ManualClock` for tests; `DisciplinedClock`, the extension point
for a GPSDO (implement `read_reference()` returning the lock state and the receiver's
offset estimate; it reports source `gpsdo` and the holdover). No `DisciplinedClock` is
shipped; the CLI's `--clock gpsdo` only changes the reported source.

The agent connects (PROTOCOL.md), heartbeats, runs one monitor loop per assigned
window, serves capture requests concurrently (at most 8; more get `busy`), keeps at
most one unsent PSD frame per window, reconnects with exponential backoff and jitter
and never crashes on a server error. Its writer sends, before each capture chunk, any
waiting control message and the waiting frame of every window. `NodeAgent` options also
on the CLI: `chunk_payload_bytes` (32 KiB, 8 to 262144; the uplink must carry one chunk
well within three heartbeat periods, so about 44 kbit/s at the default heartbeat;
`--chunk-payload-bytes`), `send_buffer_bytes` (`SO_SNDBUF`; bounds how long control
messages wait behind samples already in the kernel; `--send-buffer-bytes`) and
`channel_driver_options`. Not on the CLI: `server_silence_timeout_s`,
`connect_options` (for example `ssl=` with a private CA), `proxy`, `driver_factory`.
Text from the driver or peer that is put on the wire (`hello` software and driver,
`capture_error.message`, a channel error in `assign_ack`) has characters that are not
valid Unicode escaped (`backslashreplace`). A `sim_update` is handed only to drivers
already open (one that arrives while channels open is lost on the node; the hub re-sends
it after the ack, PROTOCOL.md A). Without `node_id` or `state_path` the node id is kept
in `$XDG_STATE_HOME/petal-locate/node-<site>.json` (else `~/.local/state/...`).

## 7. Server (`server/`)

FastAPI application created by `create_app(settings, *, hub_providers=None,
presets_dir=None, static_dir=None) -> FastAPI` and served by `run(settings)` or
`build_server(app, settings)` (a uvicorn server that closes the hub before uvicorn
closes sockets, so nodes see 1001; `ws_per_message_deflate=False`; `ws_max_size`
1 MiB; log filters that drop inbound text-frame traces (the node `hello` carries its
token) and rewrite `?token=` as `?token=[redacted]`). The lifespan starts the hub,
builds the `ServerRuntime` on the serving event loop, freezes the start-up heap for
the garbage collector (`gc.freeze`: generation-2 collections of 80 to 150 ms broke the
50 ms budget) and sets Python's thread switch interval to 1 ms; both are undone at
shutdown. `GET /healthz` is a liveness probe without authentication. The server's time
is `app.state.clock = Settings.clock()` (the host clock, plus `sim_time_offset_ns` under
`petal-locate demo --start`): the hub (`Hub(clock=...)`), the runtime
(`ServerRuntime.from_settings(store=..., clock=...)`), the store (built in the lifespan
with the settings' retention limits) and rest's default `t` use it. (`app._hub_clock`
and `_ShiftedTime`, which patched the hub module's `time` for a hub without a `clock`
parameter, no longer act: the hub takes the clock.)

`RequestGuardMiddleware` runs before any route (PROTOCOL.md B): the `Host` check
(`allowed_host_names(settings)`; exempt `/ws/node`, `/healthz` and the backplane; 400
or a refused handshake), the request-kind check on state-changing `/api` requests (403
unless the `Content-Type` is not a CORS-simple one, or `Authorization` or
`X-Petal-Client` is present; `app.CLIENT_HEADER = "x-petal-client"`) and the 4 MiB body
cap on those requests (413). The `/ws/ui` handshake checks `Origin` (`deps.ws_origin_ok`;
1008). The console page served for `/?token=<api token>` carries a script that stores
the token in `sessionStorage` (`app.CONSOLE_TOKEN_KEY = "petal.apiToken"`) and strips it
from the address (`no-store`, `no-referrer`); static paths holding NUL or failing to
resolve are 404.

`Settings` (frozen pydantic): `config_path`, optional `scenario_path` (without it the
server does not simulate), `data_dir` (default `data`), `host` (default 127.0.0.1),
`port` (default 8080; 0 lets the OS choose), `node_tokens: dict[site_id, token]`,
`node_tokens_path` (`--node-tokens FILE`, a YAML map read with `safe_yaml_load`; when
set its current content replaces `node_tokens` and it is re-read at the next lookup
whenever its modification time, size or inode changed; a file that no longer loads is
logged and its last good tokens kept; a site absent there falls back to the environment
variable `PETAL_NODE_TOKEN_<SITE_ID>`, the site id upper-cased with every character
outside `A-Z 0-9` replaced by `_`, read at lookup time; two sites mapping to one variable
are refused at start-up; a site with no token cannot connect; `demo` generates random
per-run tokens), optional `api_token` (when set, every `/api` request except `/meta`
and the `/ws/ui` handshake must carry it; `serve` warns when there is none, `demo`
makes a random one per run), `allowed_hosts` and `allowed_origins` (tuples, lower-cased,
trailing `/` stripped, `*` for any), optional `marking: {level, line}` for the console
banner (deployment configuration, never inferred), `heartbeat_s` (2), `hello_timeout_s`
(10), `capture_grace_s` (1), `capture_limit_bytes` (> 0, `DEFAULT_CAPTURE_LIMIT_BYTES` =
512 MiB), `history_max_age_s` (> 0 or None), `history_max_events` (>= 1 or None) and
`sim_time_offset_ns` (0; host clock plus it must stay within 1970..2262; simulation
only). Methods `now_ns()`, `clock()` (`time.time_ns` itself when the offset is 0),
`node_token(site_id)`, `sites_without_token(site_ids)`, `check_node_token`,
`check_api_token`; constant `LOCAL_HOST_NAMES = ("localhost", "127.0.0.1", "::1")`.
Tokens are compared in constant time over SHA-256 digests and kept out of every `repr`
and log line.

- `hub.py`: PROTOCOL.md section A from the server side, independent of the
  configuration models (it reads `HubProviders`: assignments, version, simulation
  object). One writer task per socket owns sending, so the receive loop never waits on
  a slow node; the receive loop yields one event-loop turn per frame. `await
  hub.capture(channel_id, t_start_ns, n_samples, sample_rate_hz, center_hz,
  deadline_ns) -> AssembledCapture` (`iq`, `t_first_ns`, `sample_rate_hz` / `fs`,
  `center_hz` / `fc`, `overflow`, `clock`, `clock_locked`), raising
  `CaptureFailed(code, message)` with codes `not_ready`, `limit`, `offline`,
  `disconnected`, `replaced`, `reassigned`, `deadline`, `bad_data`, `shutdown` (all
  retryable) and the node's `capture_error` codes; `await hub.reassign(site_ids=None)`
  after every configuration change; `announce_sim_update(version, effective_ns)`;
  `channel_capabilities(channel_id)` (None while the channel is not ready);
  `subscribe_psd` (newest-wins mailbox per (channel, monitor)) and `subscribe_status`
  (every event in order; beyond 64 waiting for one subscriber, thinned to the newest per
  site); `node_status(site_ids)`; `online_sites()`. Event-loop hold per binary frame:
  median 0.19 ms, worst 7.7 ms during a 64 MiB capture. `Hub(settings, ..., *,
  clock=time.time_ns, psd_future_tolerance_s=PSD_FUTURE_TOLERANCE_S)`: `clock` stamps
  `welcome` and `heartbeat_ack`, `PsdFrame.received_ns`, `StatusEvent.t_ns`, connection
  times and capture deadlines; a spectrum frame more than `PSD_FUTURE_TOLERANCE_S`
  (60 s) ahead of it is dropped (`psd_dropped_future`, one warning per session). The hub
  keeps the newest `SimUpdate`; a session has `awaiting_ack` from `welcome` and each
  `assign` until the accepted ack, and an update sent meanwhile is sent again after the
  ack (`sim_update_resent`). `set_window_health(provider)` (a
  `WindowHealthProvider = Callable[[channel_id, monitor_id], Mapping | None]`; a failing
  provider is logged and counted as `window_health_errors`) feeds the `windows` and
  `detection_problem` of each channel in `node_status`; a channel of an online node with
  a blind window is not `ok`.
- `runtime.py`: `ServerRuntime` owns the configuration and its integer version (the
  ETag), the scenario and its version, the store, the hub providers, the pipeline and
  `WorkPool` (3 threads for CAF, solves, classification, accuracy maps, lines of
  position, validation and large serialisations). `replace_config` checks `If-Match`
  (`*` matches any version; a value that is not a version is a conflict), validates
  with field errors, refuses a configuration the running scenario no longer fits (loc
  `(scenario)`), persists, calls `hub.reassign()`, rebuilds the pipeline, tells the
  console and, when simulating, raises the scenario version and sends `sim_update`.
  `ConsoleBus` delivers `status` once a second (the newest at once to a new client),
  events, `config`, `sim` and spectrum frames, newest wins per kind and key per client.
  A loop probe every 100 ms logs any lag over 100 ms and reports its statistics at stop
  (`runtime stopped (event loop lag: worst ... ms, n of N probes over 50 ms)`); measured
  worst 40 to 66 ms with the seven-node demo. The backplane document is served at
  `SIM_BACKPLANE_PATH = "/node/v1/sim"`. A restart on the same data directory resumes:
  the newest configuration saved through the API wins over the command-line file
  (logged), and up to 500 events of earlier runs load as `lost` history ("server
  restarted: kept as history") that never takes part in identity. Event ids are
  `EV-000001`, sequential across runs. On a node's `online` status the runtime calls
  `detection.reset_site(site_id)`; `start()` installs
  `hub.set_window_health(detection.window_health)`. `check_orbits(now_ns=None) ->
  [SatelliteStatus]` evaluates every satellite's orbit at the server clock every
  `ORBIT_CHECK_S = 30` s from the status loop and on a configuration change (WARNING
  once when one becomes unavailable, INFO when it is back); `Status.satellites` and
  `Status.site_clocks` (from `Tasking.site_clock_estimates()`) are published with the
  status. A scenario load calls `Tasking.world_changed(effective_ns)`.
- `store.py`: one SQLite file (`petal-locate.sqlite3`, WAL) in the data directory, one
  connection on its own writer thread, every operation a short transaction in submission
  order; the event loop never waits on the disk. Tables: config versions (source `file`
  or `api`), scenario versions, events (the whole console document), transitions,
  evidence snapshots, measurements, solutions, calibrations, node sessions, captures,
  runs. Pipeline writes are fire-and-forget (failures logged and counted); beyond 4096
  waiting writes a write is refused with `StoreError`. Every completed IQ capture is
  written as a SigMF pair (`cf32_le`) under `data_dir/captures`; the oldest are deleted
  beyond `capture_limit_bytes` of data files (`Settings.capture_limit_bytes`, `serve
  --capture-limit-bytes`, default `DEFAULT_CAPTURE_LIMIT_BYTES` = 512 MiB).
  `Store(..., history_max_age_s=None, history_max_events=None)`: `prune_history() ->
  PruneResult(events, calibrations, node_sessions)` deletes events not updated for
  longer than the age and those beyond the count (least recently updated first), with
  their transitions, evidence snapshots, measurements and solutions, plus calibrations
  older than the age and closed node sessions that ended before it; it runs when the
  store opens and at most every `PRUNE_INTERVAL_S = 60` s while events are written, and
  logs `history retention: deleted ...`. Properties `capture_limit_bytes` and
  `history_limits`. Events still in memory are not affected until the runtime's own
  pruning drops them.
- `pipeline/detection.py`: one `SignalTracker` per (channel, monitor), built by
  `build_window_specs` (in a worker thread): passbands of every watched satellite's
  transponders overlapping the window with their `relay_rise_db` (evaluated when the
  configuration is installed; 0 dB for a satellite below the horizon then, and not
  updated as satellites move), and the authorised carriers mapped through **every**
  watched satellite whose transponder relays the carrier's centre, each widened by
  `downlink_spread_hz` (MATH section 7) and rounded outward to whole bins. A tracker is
  rebuilt only when its inputs change (it then relearns its baseline; the old tracker's
  detections are carried for `confirm_n` frames so the event is not lost meanwhile;
  track ids are namespaced by tracker generation). Every tracker update runs on one
  dedicated spectrum thread (on the loop's own thread each update waited for the GIL
  behind the CAF and solver threads and held the loop up to 251 ms); without that thread
  (tests, tools) a window whose median update cost exceeds 3 ms, or with `nfft >=
  16384`, is updated in the worker pool. Each accepted frame gives a `WindowReport` to
  the correlator and, for streamed windows, the console spectrum frame. Frame times: a
  frame up to `CLOCK_STEP_FRAMES = 3` frame periods older than the newest accepted is
  refused (repeated or out of order); an older one is a clock step, accepted as the new
  time base (`tracker.reset_time()` in the update thread; counter `clock_steps`, one
  WARNING per window). `reset_site(site_id) -> int` forgets a site's frame times on a
  new session (baselines and tracks kept; counter `site_resets`). `window_health(channel,
  monitor) -> dict | None` reports `detection_frame_age_s`, `blind` (frames arriving,
  the newest within 3 periods, but none accepted for `BLIND_FRAMES = 10` periods; WARNING
  once, INFO on recovery) with `blind_reason`, `learning`, `floor_raised_db` and `note`.
  `floor_raised_db` is set when the window's `SignalTracker.floor_db` is more than
  `detection.threshold_db` above the lowest floor of the channel's other windows of the
  same span that accepted a frame within 30 s, and clears 1 dB below
  (`FLOOR_RAISED_HYSTERESIS_DB`), with a WARNING when set and INFO when cleared.
- `pipeline/correlator.py`: uplink mapping, event identity with merge, split and
  re-open, evidence rows with predicted margins, capability, the verdict with settle
  time, hysteresis and the uplink-to-local override, and expiry of silent windows (MATH
  section 7, which states the rules and constants). `tick()` runs once a second. A
  detection unit that overlaps no active event is held, with its frame time, until
  every window that delivered a frame within `stale_frames` periods has reported a
  frame stamped at or after it, or for `NEW_EVENT_HOLD_S = 1.0` s
  (`Correlator(new_event_hold_s=...)`); held units are then placed by overlap group
  (join an active event, be absorbed by a closed event still holding detections, reopen
  a lost one, or create one event), so fragments of one wideband signal raise one event
  (counter `held`). Once a consistent fit exists, satellites that cannot see the fix,
  an end of either ellipse axis or any ambiguity (`events.visible_satellites`, cached
  per solution) leave `satellite_ids` and `transponders` for
  `not_visible_satellite_ids` (never every satellite).
- `pipeline/events.py`: `EventRecord` and `EventBook` (lifecycle below, transitions,
  publication, the mutation API for tasking: `mark_characterizing`,
  `set_classification`, `add_measurement`, `set_caf`,
  `set_solution(..., *, max_reduced_chi2=None)`, `report_cross_satellite_detection`,
  `set_tasking`, `set_aside(..., keep=())`, `mark_recheck(event_id, reason)`,
  `clear_recheck(event_id, reason)`, `transmission_changed(event_id, why,
  onset_ns=None, keep=())`), conversion to `schemas.Event`, simulation truth matching.
  `assess_fit(chi2, dof, reduced_chi2, max_reduced_chi2) -> FitAssessment(consistent,
  p_value, scale)` (`FIT_MAX_P_VALUE = 1e-3`) and `visible_satellites(solution, orbits,
  sat_ids, t_s, min_elevation_deg)` are public. `EventRecord` gains `onset_ns`,
  `resumed_ns`, `break_s`, `recheck`, `recheck_since_ns`, `set_aside_count` and
  `not_visible_satellite_ids`, `received_range()` and `onset`. Mutations never block and
  never raise for an event that has moved on, and follow merges. Publication: material
  changes (a re-check included) at the next flush, numeric drift at most every 5 s.
  Caps: 1000 measurement rows, the first
  and newest 500 solution-history rows and 200 transitions per document (the store
  keeps all); `prune()`, called from the runtime's 1 Hz loop, drops the oldest inactive
  events beyond 1000 from memory (closed with no detections, lost longer than the
  re-open window, or history), after which `GET /events/{id}` answers 404 although the
  store still holds them.
- `pipeline/tasking.py`: one *round* per taskable event (active, in a transponder
  passband, verdict `uplink` or `undetermined`) every `capture.interval_s`; at most 4
  rounds in flight; scheduling tick 0.5 s; rounds are cancelled on event close,
  configuration change and stop.
  - Channels. Primary: the event's sighting with the highest `snr_db`; the previous
    round's primary stays while within `PRIMARY_HOLD_DB = 3` dB of the strongest (a new
    primary makes every pair new and unconfirmed). For each other satellite whose
    transponder overlaps the uplink range and that is not in the event's
    `not_visible_satellite_ids` (note: `X cannot see the transmitter's position, so it
    is not paired`): the channel watching it with the highest
    `downlink_quality_db` (table refreshed every 30 s), preferring a channel at the
    primary's site within 3 dB (common clock). A channel must be online, acknowledged,
    able to tune the centre, not excluded by an earlier non-retryable failure, and its
    pair must have an admissible emitter position (`measurement_windows` not None).
    Partner rotation: in the 4th, 6th, 8th ... rounds of an event each partner slot is
    handed, in turn, to another candidate within `ROTATE_WITHIN_DB = 1` dB of the best
    quality (ordered by channel id); the other rounds keep the documented choice, so
    the time to first fix is unchanged. Channel qualities typically differ by fractions
    of a dB, so without rotation the same sites were always used and a fault at an
    unused site (a late clock) never showed. Alternates need their own repeat detection
    and calibration and are first used after about 35 s.
  - Capture plan. `f_e_nom = round((f_lo + f_hi) / 2)` Hz; `sample_rate_hz =
    min(max(capture.min_sample_rate_hz, capture.oversample * (f_hi - f_lo), 2.2 x the
    largest |Doppler| searched), the lowest usable maximum)`, identical in every request
    of the round; `center_hz = f_e_nom + translation_hz` of each channel's transponder;
    `n = min(round(capture.duration_s * fs), capture.max_samples)`, shortened when the
    bounded dynamics would break the CAF drift assumption (MATH section 6; the duration
    is at most `1 / sqrt(rf / RATE_SAFETY)` with `rf` the FDOA-rate bound below), lengthened
    when a delay window plus the estimator's guard (24 lags + 8 main lobes) needs it
    (capped at `max_samples`, with a note), and at least 1024 (what `classify` needs);
    `t_start_ns = server now + max(capture.lead_s, min(3 x the worst heartbeat round
    trip, MAX_LEAD_S = 4 s))` (the round trip includes time queued behind sample
    uploads, hence the cap); `deadline_ns = t_start_ns + (n / fs + capture.timeout_s) *
    1e9`. Every capture of the round is requested at once.
  - Calibration plan (`calibration.enabled`). A pair not calibrated for
    `calibration.interval_s` also captures a reference carrier in a later time slot of
    the same channels (one radio, one tuning; slots 50 ms apart): one slot per reference
    carrier, all its channels at `min(max(min_sample_rate_hz, oversample x carrier
    bandwidth), lowest usable maximum)` and `capture.duration_s` (lengthened for the
    window). Eligible references: relayed by both of the pair's transponders, not
    overlapping the event's uplink range, above the elevation mask from both
    satellites; with a consistent fix the one nearest the fix (geodesic distance, then
    frequency) is used, without one the nearest in frequency (`_references_for(...,
    near)`, `_plan_references(..., near)`). The correction from
    `core.solver.calibration_correction` is held per `(a.sat_id, a.site_id, b.sat_id,
    b.site_id, kind)` **and reference carrier**, sign-flipped for the reversed pair,
    valid for `calibration.max_age_s`, each with its own `cal_id`, stored; it is applied
    to a target row only when its carrier is relayed by that row's transponders. Events
    calibrated on different reference carriers through the same channels each keep
    their own and are scheduled separately, every `calibration.interval_s` per carrier.
    Reference results get no repeat check (their window is a small box around the
    prediction). A reference capture that fails is retried only after
    `calibration.interval_s`. Calibrations, confirmation tracks and channel exclusions
    live in memory: a restart or a configuration change starts them afresh
    (calibrations are stored, not reloaded), and `Tasking.world_changed(effective_ns)`
    (a scenario load) drops the calibrations held and any measured before
    `effective_ns` by a round in flight.
  - Geometry. A round evaluates only the satellites it needs (the picks plus every
    satellite relaying the event's range or a planned reference,
    `Tasking._satellites_needed`), each on its own: one whose orbit cannot be evaluated
    is treated as below the horizon, its pairs and references are skipped (`satellite
    X: orbit not available (...)` in the round's notes) and the status message names
    it; the other pairs go on.
  - Correlation (`caf_estimate` in the worker pool): windows from
    `measurement_windows` + `search_windows`, shifted by a valid calibration (fixed
    margins only), otherwise widened by the systematics and the ephemeris allowance
    `6 (|sigma_pos,a| + |sigma_pos,b|) / c` in delay and `6 f (|sigma_vel,a| +
    |sigma_vel,b|) / c` in Doppler (3 sigma on each of a satellite's two legs; MATH
    section 6); `exclude` from `self_peak` for every
    satellite heard by both channels (above both horizons, relaying `f_e`, downlink band
    overlapping both capture bands; the antenna pattern is ignored) and the direct-path
    point when both channels are at one site with equal translations; `mirror_window`
    for the swapped pairing when the two satellites are co-channel, both are seen from
    the other site and both cross copies fall in the other channel's band;
    `signal_bw_hz = max(f_hi - f_lo, 10 Hz)`; `t0_offset_s` from the integer difference of
    the first-sample times; centre differences corrected per MATH section 5. Target and
    reference rows carry capture ids (`<channel_id>|<t_first_ns>|<round centre>|<round
    rate>`) and the estimator's noise shares, so the solver models the noise every pair
    of a round shares through the primary's capture.
  - Rejection. A pair is rejected (two rows with null values and the reason) for a
    returned rate other than the request (relative 1e-9), lost samples (`overflow`), a
    window the correlator refuses, or a non-retryable capture failure (`unknown_channel`,
    `too_large`, `out_of_range`, `invalid_request`, which also exclude that channel for
    the event). Retryable failures add no rows; they show in `Event.tasking.message`. An
    unlocked clock drops the TDOA between different sites (`clock_unlocked`) and keeps
    the FDOA. CAF results that are undetected, ambiguous, coupled, outside the window or
    mirror-ambiguous are recorded with their reason and never solved.
  - Confirmation. A pair's rows enter the solver, and the cross-satellite detection is
    reported to the verdict, only once a detection is repeated at a consistent
    TDOA/FDOA (MATH section 6; the gate adds the largest change the satellites' motion
    allows, `Tasking._rate_bounds`: FDOA rate `RATE_SAFETY x (|f| (r_a + r_b) + |f +
    T_a| r_a + |f + T_b| r_b) / c` with `r = |a| + |v|^2 / r_min` per leg and `r_min`
    the geocentric radius less 6378137 m less the highest site, mast or emitter height,
    floored at 100 km); until then they carry the `unconfirmed: ...` reason. A round's
    confirmations are decided together (`Tasking._track_decisions`): a pair whose
    detections repeat at a value its confirmed track cannot reach has *jumped*; with no
    pair of the round agreeing, another transmitter holds the frequency and
    `EventBook.transmission_changed` sets the old rows and position aside (the jumped
    pairs' new tracks are used at once; counter `transmission_changes`); with another
    pair agreeing, the jumped pair is refused as `inconsistent with this pair's earlier
    detections` and the event is re-checked. While re-checked one agreeing pair clears
    the re-check and one disagreeing detection with none agreeing sets everything
    aside. Re-check triggers besides a short break: a change between two definite
    classes (both confidence at least `CLASS_CHANGE_CONFIDENCE = 0.5`), a primary-level
    step of `POWER_STEP_DB = 6` dB, a width change by `WIDTH_STEP_RATIO = 2` and at least
    `WIDTH_STEP_MIN_HZ = 1 kHz`. Backstop (`Tasking._split_test`): when rows added to a
    consistent fit break it (no solution or an inconsistent one) and the rows added
    since (at least `SPLIT_MIN_ROWS = 4`) fit one transmitter alone, the earlier rows are
    set aside (`set aside: the newest measurements fit one transmitter of their own but
    not together with the earlier ones`) and the new rows' fix is attached. A set-aside
    made elsewhere clears tasking's tracks (through `EventRecord.set_aside_count`).
  - Rows. Every correlated pair gives a TDOA row and an FDOA row; `value` is `z - c`
    when calibrated (with `correction` and `reference_carrier_id`), otherwise `z`.
  - Classification of the primary capture over the event's own band
    (`CLASSIFY_BAND_FACTOR = 4` times its width, at least `2 x MIN_CLASSIFY_SAMPLES`
    samples, cut out of the spectrum, so that a neighboring carrier inside a
    minimum-rate capture is not classified in the event's place): on the first round,
    15 s after an `unknown`, every 60 s otherwise, and again in every round while the
    event is re-checked. A swept class with fewer than 5 sweep periods in the capture
    sets the event's capture length to 5.25 periods (capped by `max_samples`) and
    schedules the next round at once.
  - Solve: the newest `solver.max_measurements` accepted rows; the global search on the
    first solve, every 10th and whenever a warm start fails or has `reduced_chi2 > 3`;
    warm starts from the previous solution and its ambiguities. Rows naming a satellite
    or site that no longer exists are left out; rows measured under an older
    configuration are re-solved with the new orbits. The result goes to `set_solution`
    with `processing.solver.max_reduced_chi2`: a fit with `reduced_chi2` above it and a
    chi-square tail probability below `FIT_MAX_P_VALUE = 1e-3` is published flagged
    inconsistent, its ellipse, CEP and covariances enlarged by `sqrt(reduced_chi2)`
    (counter `solves_inconsistent`), and never makes the event `located` (a located
    event goes back to `locating`). The strongest detected correlation surface of the
    round (else the latest) goes to `set_caf`.
  - Status (`Event.tasking`): a plain sentence when every correlated pair of a round
    failed for one reason of the signal (no TDOA on a narrowband signal, ambiguous
    correlations of a periodic waveform, coupling, no copy found), kept while the next
    round runs; a note for a satellite whose orbit is not available and for an
    inconsistent fit; `idle` with a message for an event judged `local`;
    `unlocatable_reason` (`UNLOCATABLE_AMBIGUOUS`, `UNLOCATABLE_COUPLED`: every pair of
    the latest round failed that way and no row has been used since;
    `UNLOCATABLE_ONE_SATELLITE`: fewer than two relaying, visible satellites), cleared
    when a row is used.
  - Site clocks: `Tasking.site_clock_estimates()` turns the TDOA calibrations still
    valid into `ClockEdge(sat_a, site_a, sat_b, site_b, c_s, eph_s, sat_sigma_a_s,
    sat_sigma_b_s)` rows (same-site ones included); `site_clock_offsets(edges, config,
    site_name=None)` fits `c = o_a - o_b + e_sat_a - e_sat_b` per connected group of
    sites by weighted minimum norm (clocks weighted by their configured sigma,
    satellites by position sigma over c, so what the data cannot resolve goes to the
    satellites), shifts each group to a median of zero, and sets `caution` when `|o| >=
    max(CLOCK_CAUTION_NS = 1000, 10 x clock.sigma_ns, 3 x the RMS site-dependent
    ephemeris allowance of its edges)` (the allowance: position sigma times the angle
    between the two sites seen from the satellite, over c); in a group of two sites both
    are flagged on their difference. Published as `Status.site_clocks`.
  - Known limits, measured against `SimWorld`: a CW has no delay to separate copies by,
    so when a channel also hears a second copy a few Doppler bins away (a co-channel
    satellite, or a self-peak ridge) the FDOA scatters up to about 5 reported sigma; two
    transmitters on the air at once and detected alternately are not separated (their
    mixed fit is flagged inconsistent, or the solve fails and the earlier position stays
    with only `solve_reason` changed); the FDOA-rate bound is a strict worst case, so in
    low orbit the repeat gate is wide and captures short (about 58 ms on
    `leo-example.yaml`).
- `pipeline/advisory.py`: plain-language headline, detail and actions (uplink: change
  channel, transponder or satellite, moving the terminal will not help; local: only
  that site is affected, relocate, shield or filter; undetermined: what evidence is
  missing; also lost, closed and merged events). Terms are explained on first use;
  simulated events have a headline starting `Simulation: `. Every report action names
  the start time (the onset of the current transmission). Ellipses are given as 95 %
  semi-axes (`95 % ellipse with semi-axes A by B, major axis N° from north`). An
  inconsistent fit is described as unreliable (no meaningful ellipse above 2,000 km) and
  its position is not to be reported; an unresolved ambiguity lists every candidate and
  says the system cannot choose; a re-checked position is reported only once new
  measurements confirm it; an unlocatable signal gets `No position can be computed.
  <unlocatable_reason>`. Local and undetermined advice quotes the received (downlink)
  frequency, the site and the channels (`received near F at SITE on CH`), with a
  `tune to F` hint. Before a fix the advisory names only satellites the signal was seen
  or measured through (`It may also reach X ...`); after one, satellites that cannot see
  the fix are said to be unaffected.
- `api/`: the console interface of PROTOCOL.md section B (`schemas.py` the contract,
  `rest.py` the routes and error mapping, `ws.py` `/ws/ui`, `deps.py` authentication and
  the `/ws/ui` origin check `ws_origin_ok`).
  FastAPI serves OpenAPI at `/openapi.json`. `python -m petal_locate.server.api.schemas`
  prints the console JSON Schema the console's TypeScript types are generated from.
- Serves the built console from `server/static/` at `/` (hashed assets cached as
  immutable; a placeholder page when no build exists).

Event lifecycle. `state` is derived: `closed` once the operator closed it (or it was
merged into another event); otherwise `lost` while no detection holds it; otherwise its
progress: `detected` (raised) -> `characterizing` (first capture requested) ->
`locating` (first CAF measurement accepted) -> `located` (a converged and consistent
solution exists; an inconsistent one sends a located event back to `locating`).
Progress is kept while an event is lost, so an event detected again within
`REACQUIRE_KEEP_S = 30` s resumes with its measurements, solution and verdict, but
with a position or used rows it is put under re-check (`recheck`, transition `detected
again after a break of N s; the earlier position is checked against new measurements
before it is kept`). Every reopen records `resumed_ns`, `break_s` and `onset_ns` (the
earliest first-seen time of the new detections; `first_seen_ns` stays the event's first
detection). After a longer loss (a re-open, up to 900 s) the solution, accepted
measurements, class and correlation surface are **set aside** (`EventBook.set_aside`):
rows stay listed with `used = false` and the reason `set aside: ...`, the ellipses stay
in the history marked `set_aside`, progress returns to `characterizing` (to `locating`
when rows passed in `keep` remain), the verdict re-settles from `undetermined`, the
re-check and `not_visible_satellite_ids` are cleared, and a solve that started before
the set-aside cannot attach its result. The same happens when the correlator turns an
`uplink` verdict to `local` (MATH section 7) and when tasking finds another transmitter
on the frequency (`transmission_changed`, which also sets `onset_ns`). A local event
reads `characterizing` because tasking captures once before the verdict settles. Every
transition is stored and pushed with a reason.

## 8. CLI (`cli.py`)

```
petal-locate serve      --config C [--scenario S] [--host H] [--port P] [--data-dir D] [--node-tokens FILE] [--api-token T]
                        [--marking-level L] [--marking-line TEXT] [--presets-dir DIR] [--access-log]
                        [--allowed-host NAME ...] [--allowed-origin ORIGIN ...] [--capture-limit-bytes N (536870912)]
                        [--history-max-age-s S] [--history-max-events N]
petal-locate node       --server URL --site ID [--token T] --driver sim|sigmf|soapy|noise [--node-id ID] [--state-path F]
                        [--driver-option KEY=VALUE ...] [--channel-option CHANNEL:KEY=VALUE ...] [--node-config FILE]
                        [--sigmf-path F] [--soapy-device ARGS] [--chunk-payload-bytes N] [--send-buffer-bytes N]
                        [--clock ntp|gpsdo|free|sim] [--clock-unlocked] [--sim-time-offset-ns NS]
petal-locate demo       [--config C] [--scenario S] [--host H] [--port P] [--data-dir D] [--presets-dir DIR] [--driver sim|noise]
                        [--node-log-level L] [--start ISO] [--no-api-token]
petal-locate check      --config C [--scenario S] [--time ISO]
petal-locate accuracy   --config C (--request FILE | --pair A,B ... --f-e-hz F [--eirp-dbw --bandwidth-hz --waveform])
                        [--step-deg] [--region LAT_MIN,LAT_MAX,LON_MIN,LON_MAX] [--kinds tdoa,fdoa] [--reference-carrier ID]
                        [--integration-s] [--time ISO] [--max-cells N (200 000)] [--out FILE]
petal-locate montecarlo --config C --scenario S --trials N [--emitter ID] [--seed N] [--draw-errors] [--param KEY=VALUE ...]
                        [--out FILE]
every command: [--log-level debug|info|warning|error] [--log-format text|json]
```

- Exit codes: 0 success, 2 usage or configuration error (a file that does not load or
  validate, an unknown driver option, a missing token), 1 runtime failure. Logs are
  structured (`key=value` after the message, or one JSON object per line) on standard
  error.
- `serve` validates the configuration and scenario before starting, warns about sites
  without a node token and (`no API token ...`) about a missing API token, takes the
  API token from `--api-token` or `PETAL_API_TOKEN`, binds the socket itself (a port in
  use is exit 2) and logs `serving <name> on http://<host>:<real port>/` with extra
  `port=<real port>`, and exits 0 after SIGINT or SIGTERM. `--presets-dir` defaults to
  the shipped `config/scenarios`. `--node-tokens` is re-read whenever the file changes.
  `--allowed-host` and `--allowed-origin` feed `Settings.allowed_hosts` and
  `allowed_origins`; `--capture-limit-bytes`, `--history-max-age-s` and
  `--history-max-events` the store's retention.
- `node` takes the token from `--token`, `PETAL_NODE_TOKEN_<SITE_ID>` or
  `PETAL_NODE_TOKEN` (the environment keeps it out of the process list). Driver options
  are YAML scalars; `--sigmf-path` and `--soapy-device` are shorthands for the `path`
  and `device` options; `--channel-option CHANNEL:KEY=VALUE` (repeatable) adds options
  for one channel; `--node-config FILE` is YAML with the keys `driver_options`,
  `channels` (`{channel_id: {options}}`), `chunk_payload_bytes` and
  `send_buffer_bytes` (`NODE_CONFIG_KEYS`; read with `safe_yaml_load`; flags win over
  the file; `load_node_config`, `node_driver_options`, `node_channel_options`). The
  shared options alone, and the shared options with each channel's, are each used to
  build a driver before connecting, so a bad option exits 2 naming the channel, and a
  driver option the driver requires (sigmf `path`) must be among the shared ones.
  Driver hardware libraries (the SoapySDR bindings of `soapy`) load only when a channel
  opens (the channel is then reported failed in `assign_ack`). `--clock` replaces the
  reported clock source (`SystemClock(source, locked=not --clock-unlocked)`); with
  `--driver sim` and no `--clock` the node runs on `sim_clock()` (source `sim`);
  nothing disciplines the clock. `--sim-time-offset-ns` (only with `--driver sim`)
  adds an offset to the node clock. A hidden `--exit-with-parent PID` stops the node
  when that process is gone.
- `demo` runs the server in-process on the example configuration and scenario (or
  `--config`, `--scenario`), with random per-run node tokens, a random per-run API
  token (`--no-api-token` runs without one and logs a WARNING), the marking
  `DEMO_MARKING_LINE = "UNCLASSIFIED"`, a temporary data directory removed on exit
  (unless `--data-dir`), presets from the scenario's directory, and one `petal-locate
  node` subprocess per site over real WebSockets (each in its own session, given its
  token in the environment and the demo's pid; restarted up to 3 times if it exits).
  It prints `Console: http://<host>:<port>/?token=<token>`, `API token: <token> (the
  address above carries it; enter it if the console asks)` and `All N nodes are
  online.`, and stops every child on SIGINT or SIGTERM. `--start ISO` (needs `--driver
  sim`, 1970 to 2230) sets `Settings.sim_time_offset_ns` to the epoch minus the host
  clock (`demo_time_offset_ns`), passes the same offset to every node
  (`--sim-time-offset-ns`) and prints `Start: simulated time starts at <ISO>`.
- `check` prints the satellites with their sub-points, look angles of every channel to
  every satellite (gain, electrically-small flag, below-horizon note), monitor windows
  (derived or configured), the link budget of every carrier into every channel, and the
  configuration warnings at `--time`. A satellite whose orbit does not cover `--time`
  is reported (`satellite X: orbit not available at this time; its paths are left
  out`), a carrier whose budget fails gets `not computed: <reason>`, and the command
  still exits 0.
- `accuracy` calls `core.accuracy.accuracy_map` (in latitude bands sized by
  `accuracy_band_cells`, with progress, after `rest.accuracy_problems`) and writes the
  `AccuracyGrid` keys as JSON; `--request` is read with `safe_yaml_load`.
- `montecarlo` calls `sim.montecarlo.run_trials` (default emitter: the scenario's only
  jammer; `--param` checked against `run_trials`' signature), prints the summary, and
  with `--out` writes the whole result.

## 9. What is and is not proven here

Everything runs against a physics simulator. The simulator and the solver share the
geometry code, so the tests prove the signal processing, estimation, protocol and
system logic, and they prove self-consistency of the model, not agreement with real
satellites (an independent from-scratch implementation of MATH.md sections 2 to 5, built
without `core.geometry`, `core.orbits` or `core.linkbudget`, agrees with `render_iq`).
`docs/LIMITATIONS.md` lists what is not proven or not done, including everything this
section required in revision 3: illustrative link and frequency-plan numbers, nominal
orbital slots, no real-hardware validation of drivers, UT1/polar-motion
simplifications, linear transponder model, no terrain model, stationary-emitter
assumption, CAF dynamics limits for low orbits, order-of-magnitude ephemeris sigmas for
TLEs, the requirement that the reference satellite's transponder overlaps the
interferer's uplink band, and that the parabolic pattern of section 5 is an envelope,
not a physical pattern (it integrates to more than isotropic power for dishes of about
1.6 to 3 wavelengths, 1.36 for 2.4 m at 260 MHz, and for large dishes, 1.10 to 1.24 at
Ku band, which is conservative for interference).
