# PETAL Locate — deploying and running it

How to set up sites, install the software, write a configuration, run the server and
the nodes, and use the console, calibration, the demo and the planning tools. Read
`LIMITATIONS.md` first: nothing here has run against real satellites or radios, the
`soapy` driver is untested on hardware, and no disciplined-clock implementation is
shipped. Every command below was run as shown (on Linux, Python 3.13) while this
document was written; the outputs quoted are from those runs. Example values (tokens,
ports, file names, the two-site network) are illustrative.

## 1. What a deployment is

- **Sites**: one per location, each with one receive channel per satellite it watches
  (an antenna, a radio and its time reference) and one host running `petal-locate node`.
  A node dials out to the server over one WebSocket, so sites need no inbound ports.
- **Server**: one host running `petal-locate serve`: node hub, event pipeline (detection,
  verdict, capture tasking, correlation, calibration, solving), SQLite store, console API
  and the console itself.
- **Reference transmitters** (optional but needed for useful accuracy, section 8):
  carriers at surveyed positions, relayed by the same transponders as the interference.
- **Operators**: a browser on the console.

## 2. Per-site hardware

**Antennas at UHF.** The legacy UHF downlinks are near 250 to 270 MHz (260 to 268 MHz in
the example plan), a wavelength of about 1.15 m. A parabolic dish focuses only when it is
several wavelengths across; under 3 wavelengths (3.5 m at 260 MHz) the configuration
check flags it as electrically small. What the antennas of the example give (from
`core.antennas`, RESULTS.md section 1):

| antenna | size in wavelengths | gain | weakest 10 kHz jammer detected (EIRP toward the satellite) |
|---|---|---|---|
| 0.72 m PETAL dish | 0.63 | 3.3 to 3.5 dBi, beam about 120 degrees | 10.7 to 11.4 dBW |
| 1.2 m dish | 1.05 | 7.7 to 7.9 dBi | 7.2 to 7.8 dBW |
| 10 dBi beam (helix, Yagi), linear, 3 dB polarisation loss | n/a | 10 dBi less 3 dB | 7.7 to 8.5 dBW |

A small PETAL dish at UHF is therefore a broad-beam reflector, not a focusing antenna:
it detects strong uplink interference (about 11 dBW toward the satellite, 13 W into an
omnidirectional antenna, for 10 kHz of noise) but buys little gain. A 10 dBi helix or
Yagi does as well as a 1.2 m dish. UHF satellite signals are circularly polarised; a
linear antenna loses 3 dB (`polarization_loss_db: 3.0`). A broad beam also hears other
satellites above the horizon; when two satellites share a downlink frequency (MUOS
Pacific and MUOS Indo-Asia in the example), the correlator excludes the predicted
cross-copies. Location accuracy does not depend on antenna gain once the signal is
detected (RESULTS.md section 4.6): any one site with channels on all three satellites
predicts within about 25 % of the seven-site network.

**Radio (SDR).** One receive stream per channel (the `soapy` driver opens one stream per
channel, and `petal-locate node` gives every channel the same driver options, so a site
with several radios needs its own launcher, ARCHITECTURE.md section 6). Requirements:
tuning over the downlink band; a complex sample rate of at least the largest monitor
span plus margin (the example's monitors are 31.25 kHz, captures 20 kHz, and
`max_sample_rate_hz` defaults to 2 MHz); continuous streaming without overflow;
**hardware timestamps** latched to UTC on a PPS edge; an external 10 MHz reference input;
SoapySDR support. The `soapy` driver keeps a 4 s lookback ring (`buffer_s`) and needs the
SoapySDR Python bindings (`python3-soapysdr`, or the `sdr` extra).

**Time: a GPS-disciplined oscillator with PPS.** Required for TDOA between sites. A
site-clock error enters every cross-site TDOA one for one (1 us is 300 m of range
difference); the configuration's `clock.sigma_ns` is 30 ns. In the simulator a 10 us
error at one site moved the uncalibrated fix 14 to 34 km, outside its own error ellipse,
with no warning from the fit (scenario `clock-fault`). NTP is not enough: it disciplines
the host's clock, not the radio's sample counter, and the samples are timed by the radio.
The radio must take its sample clock from the GPSDO's 10 MHz (FDOA needs the site
oscillator to about 1e-11, which is 2.6 mHz at 260 MHz against FDOA sigmas of about
1 mHz) and set its time on the GPSDO's PPS (`--driver-option time_sync=pps`). The node
must report the reference's lock state honestly: the shipped `SystemClock` reports
`ntp`, locked, without checking anything, and `--clock gpsdo` only relabels it. A real
site needs a `DisciplinedClock` subclass (`node/clock.py`) reading the GPSDO's lock and
offset, or the soapy driver's `lock_sensor` option; neither has been tested on
hardware. Captures from an unlocked clock give no cross-site TDOA (their FDOA is used).

**Host.** Linux, Python 3.11 or later (only 3.13 was tested), 2 worker threads per node.
A simulated node measured 237 MB resident; a real driver's needs are unmeasured.

**Network.** Outbound WebSocket to the server (TLS through the server's proxy, section
5). Spectrum frames are 4296 bytes for a 1024-bin window (measured with
`protocol.framing.encode_psd`), so at 2 frames/s each window needs about 69 kbit/s: a
site with three channels and three windows each about 620 kbit/s. A 1 s capture at the
example's 20 kHz is 160 kB; a channel used in every round with a reference slot sends
about 320 kB per 5 s round (about 0.5 Mbit/s) while an event is being located. The node
also needs at least about 44 kbit/s to carry one 32 KiB capture chunk within three
heartbeat periods, or the server takes it for silent.

## 3. Installing

From the `locate/` directory of the repository (this was run in a copy, into a fresh
virtual environment; it took 11 s with the dependencies already in pip's cache):

```bash
python3 -m venv .venv
.venv/bin/pip install .
.venv/bin/petal-locate --version
```

```
petal-locate 0.1.0
```

The wheel carries the console build and a copy of `config/` (example configurations and
scenarios), so `petal-locate demo` works from an installed package. For development use
the editable install of `CONTRIBUTING.md`. The SoapySDR bindings are not on PyPI for
every platform; install them from the system package manager. Building the console
(Node.js) is not needed: the build is committed under `src/petal_locate/server/static/`.

## 4. Writing a configuration

A configuration is one YAML (or JSON) file: satellites with their transponders, sites
with their receive channels, the authorised carriers (frequency plan, including the
reference transmitters) and processing settings. ARCHITECTURE.md section 3 lists every
field and rule; `config/pacific-uhf.yaml` is a commented example. This two-site,
two-satellite network was used for the commands below:

```yaml
version: 1
name: my-network
description: Two sites, two satellites sharing one 25 kHz UHF channel, one reference carrier. Illustrative.

satellites:
  - id: sat-a
    name: Satellite A (nominal slot 172 E)
    orbit: {type: geo, lon_deg: 172.0, inclination_deg: 4.0, phase_deg: 200.0, epoch: "2026-01-01T00:00:00Z"}
    transponders:
      - {id: ch-1, uplink_center_hz: 301.000e+6, bandwidth_hz: 25.0e+3, translation_hz: -33.6e+6,
         g_over_t_dbk: -14.0, sfd_dbw_m2: -136.0, eirp_sat_dbw: 26.0}
  - id: sat-b
    name: Satellite B (from a TLE; synthetic elements, catalogue number 99999)
    norad_id: 99999
    orbit:
      type: tle
      line1: "1 99999U 26001A   26001.00000000  .00000000  00000-0  00000-0 0  9993"
      line2: "2 99999   4.0000  80.0000 0002000 270.0000 250.0000  1.00271000    19"
    ephemeris_sigma: {pos_m: [2000, 20000, 10000], vel_mps: [0.3, 0.3, 0.7]}
    transponders:
      - {id: ch-1, uplink_center_hz: 301.000e+6, bandwidth_hz: 25.0e+3, translation_hz: -41.0e+6,
         g_over_t_dbk: -14.0, sfd_dbw_m2: -136.0, eirp_sat_dbw: 26.0}

sites:
  - id: tokyo
    name: Tokyo (city centre)
    lat_deg: 35.690
    lon_deg: 139.692
    antenna_height_m: 10.0
    time_zone: Asia/Tokyo
    clock: {sigma_ns: 30.0}
    channels:
      - id: tokyo-a
        antenna: {type: beam, gain_dbi: 10.0, hpbw_deg: 52.0, front_to_back_db: 20.0}
        pointing: {satellite_id: sat-a}
        polarization_loss_db: 3.0
        max_sample_rate_hz: 2.0e+6
        monitors:
          - {id: ch-1, center_hz: 267.400e+6, span_hz: 31.25e+3, nfft: 1024, navg: 8, frame_rate_hz: 2.0}
      - id: tokyo-b
        antenna: {type: parabolic, diameter_m: 1.2, efficiency: 0.55}
        pointing: {satellite_id: sat-b}
  - id: manila
    name: Manila (city centre)
    lat_deg: 14.599
    lon_deg: 120.984
    time_zone: Asia/Manila
    channels:
      - id: manila-a
        antenna: {type: beam, gain_dbi: 10.0, hpbw_deg: 52.0}
        pointing: {satellite_id: sat-a}
        polarization_loss_db: 3.0
      - id: manila-b
        antenna: {type: beam, gain_dbi: 10.0, hpbw_deg: 52.0}
        pointing: {satellite_id: sat-b}
        polarization_loss_db: 3.0

carriers:
  - id: ref-guam
    name: Reference carrier, Guam city centre (illustrative)
    satellite_id: sat-a
    transponder_id: ch-1
    uplink_center_hz: 300.9925e+6
    bandwidth_hz: 8.0e+3
    location: {lat_deg: 13.476, lon_deg: 144.749, alt_m: 0.0, antenna_height_m: 10.0}
    emitter: {eirp_dbw: 20.0, antenna: {type: isotropic}, pointing: omni, bandwidth_hz: 8.0e+3, waveform: noise}
    use_as_reference: true

processing:
  region: {lat_min: -40.0, lat_max: 50.0, lon_min: 90.0, lon_max: 180.0}
```

- **Satellites.** From a slot (`type: geo`: longitude, inclination, phase, epoch), a TLE
  (`type: tle`; checksums and catalogue numbers are checked), Kepler elements, or an
  operator's ECEF ephemeris table (`type: table`, the best choice when you have one).
  TLE orbits get the TLE-grade ephemeris sigmas by default (RIC 2 / 20 / 10 km), the
  others operator-grade (0.1 / 1 / 1 km); `ephemeris_sigma` overrides either. Set
  `dut1_s` from the IERS bulletin when it matters (polar motion is ignored).
- **Transponders.** Uplink centre, bandwidth, signed translation (downlink = uplink +
  translation), G/T, SFD, saturated EIRP, optional group delay. A signal is relayed when
  its centre lies in the uplink passband. Only satellites that relay the same uplink
  frequency can be paired for location.
- **Sites and channels.** Antenna phase centre position (`lat_deg`, `lon_deg`, `alt_m`
  plus `antenna_height_m`) surveyed to metres; `clock.sigma_ns` honest for the time
  reference; `time_zone` is display only. Each channel has an antenna model, pointing (a
  satellite it tracks, or a fixed azimuth/elevation with the satellites it watches),
  `system_noise_temp_k`, `polarization_loss_db` and `max_sample_rate_hz` (the radio's
  usable rate).
- **Monitors.** Leave `monitors` empty to derive one window per transponder of each
  watched satellite (1.25 times the transponder width, 1024 bins, 8 averages, 2 frames
  per second; `check` marks them `derived`), or list them explicitly (downlink
  frequencies; `navg` of at least 8 keeps the detection threshold calibrated).
- **Carriers.** Every authorised carrier of the frequency plan (so it is not raised as
  interference); a carrier with `location`, `emitter` and `use_as_reference: true` is a
  reference transmitter (section 8).
- **Processing.** Defaults suit the example (ARCHITECTURE.md section 3); set `region`
  to where emitters can be (it resolves mirror ambiguities and bounds searches).

Check it before use. `check` validates, prints the satellites' sub-points, look angles
of every channel to every satellite (with gain, the electrically-small flag and
below-horizon notes), the monitor windows, the link budget of every carrier into every
channel, and warnings; exit code 2 with every field error when it does not validate:

```bash
petal-locate check --config my-network.yaml --time 2026-01-01T03:00:00Z
```

```
Configuration 'my-network' (version 1) is valid.
  ...
sat-b      Satellite B (from a TLE; synthetic elements, catalogue number 99999)  tle            -1.72         139.27             1
  ...
    sat-a      ch-1         tokyo   tokyo-a    52.8     33.2       55.3    16.3  ch-1        yes
  ...
Warnings (1)
  sites.0.channels.1.antenna: channel 'tokyo-b': a 1.2 m dish is under 3 wavelengths at 260.000 MHz; it behaves as a reflector with a broad beam, not as a focusing dish
```

A first draft of this file put satellite B 36 degrees below both sites' horizons;
`check` said so in its warnings (`satellite 'sat-b' is 36.0 degrees below the horizon of
site 'tokyo'`). Add `--scenario FILE` to check a simulation scenario against the
configuration too.

## 5. Running the server

Node tokens are per site, in a YAML map of site id to token (quote tokens that look like
numbers), kept readable by the server's user only. An API token protects the console
API. Both were made like this:

```bash
umask 077
python3 -c "import secrets; print('tokyo: \"' + secrets.token_urlsafe(24) + '\"'); print('manila: \"' + secrets.token_urlsafe(24) + '\"')" > node-tokens.yaml
python3 -c "import secrets; print(secrets.token_urlsafe(24))" > api-token.txt
```

Then:

```bash
PETAL_API_TOKEN="$(cat api-token.txt)" petal-locate serve --config my-network.yaml --data-dir ./data \
    --node-tokens node-tokens.yaml --host 127.0.0.1 --port 18731 \
    --marking-level UNCLASSIFIED --marking-line "UNCLASSIFIED // TEST"
```

```
... INFO    petal_locate.cli: serving 'my-network' on http://127.0.0.1:18731/ (not simulating) api_token=True marking=True
... INFO    uvicorn.error: Uvicorn running on http://127.0.0.1:18731 (Press CTRL+C to quit)
```

- **Settings.** `--host` (default 127.0.0.1) and `--port` (default 8080); `--data-dir`
  (default `./data`); `--node-tokens`; a node token can also come from the server's
  environment as `PETAL_NODE_TOKEN_<SITE_ID>` (site id upper-cased, other characters as
  `_`), and a site with no token cannot connect (the server warns at start-up); the API
  token from `--api-token` or, better (it stays out of the process list),
  `PETAL_API_TOKEN`. Without `--scenario` the server does not simulate. `--log-format
  json` writes one JSON object per line; `--access-log` logs every HTTP request. Exit
  code 2 means the configuration or a flag is wrong, 1 that the server did not start.
  SIGINT or SIGTERM stops it cleanly (exit 0; nodes see close code 1001).
- **Marking banner.** `--marking-level` (for example `UNCLASSIFIED` or `CUI`) and
  `--marking-line` (the full banner text) are shown at the top and bottom of every
  console screen. They are deployment settings, never inferred; with neither there is
  no banner.
- **Checks that it works.** `GET /api/v1/meta` needs no token and shows the marking and
  whether a token is required; everything else answers 401 without the token:

```bash
curl -s http://127.0.0.1:18731/api/v1/meta
curl -s -H "Authorization: Bearer $(cat api-token.txt)" http://127.0.0.1:18731/api/v1/status
curl -s http://127.0.0.1:18731/healthz
```

```
{"version":"0.1.0","marking":{"level":"UNCLASSIFIED","line":"UNCLASSIFIED // TEST"},"simulation":false,"server_t_ns":"1794583261086754925","config_name":"my-network","api_token_required":true}
{"server_t_ns":"1794583261115582971","config_version":1,"nodes":[{"site_id":"tokyo","node_id":"e6fde880-...","online":true,"latency_ms":2.38,"clock":{"source":"ntp","locked":true,"est_offset_ns":0.0,"holdover_s":0.0}, ...
{"status":"ok","version":"0.1.0","nodes_online":1}
```

- **TLS.** The server speaks plain HTTP and WebSocket. In production bind it to
  127.0.0.1 and put a TLS-terminating reverse proxy in front of it. The proxy must pass
  WebSocket upgrades on `/ws/node` and `/ws/ui`, allow messages of 1 MiB, not buffer
  responses (spectrum and event streams), keep idle WebSockets open longer than the 20 s
  keepalive, cap connections per client (the server cannot abort a half-open
  connection), and serve `/`, `/api/`, `/node/v1/sim` and `/healthz`. Without TLS the
  node tokens and the API token travel in clear text. No proxy configuration was tested
  for this document.
- **Data directory and retention.** `--data-dir` holds `petal-locate.sqlite3` (WAL
  mode; configurations, scenarios, events with their whole history, measurements,
  solutions, calibrations, node sessions; append-only, never pruned) and `captures/`
  (every completed IQ capture as a SigMF pair; the oldest are deleted beyond 512 MiB;
  there is no flag to change that). Restarting on the same directory resumes it: the
  newest configuration saved through the console wins over the `--config` file (the log
  says so), and earlier events appear as inactive history. Back up or rotate the
  directory with the server stopped.
- **Configuration changes** made in the console are validated, saved with a new version
  and pushed to the nodes at once; an editor holding an older version gets a conflict
  instead of overwriting.

## 6. Running a node

At each site, with the site's token in the environment (where other users cannot read it
from the process list):

```bash
export PETAL_NODE_TOKEN_TOKYO="<the tokyo token from node-tokens.yaml>"
petal-locate node --server ws://127.0.0.1:18731 --site tokyo --driver noise --state-path ./tokyo-node.json
```

```
... INFO    petal_locate.node.agent: generated a node id node_id=e6fde880-dfb2-4755-a001-a53465e63f2a state_path=tokyo-node.json
... INFO    petal_locate.node.agent: welcomed by the server site_id=tokyo ... config_version=1 heartbeat_s=2.0 channels="['tokyo-a', 'tokyo-b']" simulation=False
... INFO    petal_locate.node.agent: assignment applied ... channels_ok="['tokyo-a', 'tokyo-b']" channels_failed=[]
```

- **Server URL.** `--server` takes `ws://`, `wss://`, `http://` or `https://` (mapped to
  WebSocket; `/ws/node` is added when the URL has no path). Behind the TLS proxy give the
  proxy's `https://` origin. The node logs a warning when its token would go over
  `ws://` to a host that is not loopback.
- **Tokens.** `--token`, or `PETAL_NODE_TOKEN_<SITE_ID>`, or `PETAL_NODE_TOKEN`. A
  missing token exits at once (`no token for site 'manila': pass --token or set
  PETAL_NODE_TOKEN_MANILA (or PETAL_NODE_TOKEN)`, exit 2). A wrong token is rejected
  (`server rejected the node: bad_token; retrying at the longest backoff`) and the node
  retries about every 30 s.
- **Node ids.** The node generates a UUID once and keeps it in `--state-path` (default
  `$XDG_STATE_HOME/petal-locate/node-<site>.json`, else `~/.local/state/...`), or takes
  `--node-id`. A second node with the *same* id replaces the first; with a different id
  it is refused (`duplicate_node`) while the first is connected. Run exactly one node per
  site.
- **Drivers.** `--driver soapy|sigmf|noise|sim`, options as `--driver-option KEY=VALUE`
  (YAML scalars; unknown options exit 2).
  - `soapy` (real radios; untested on hardware): `--soapy-device ARGS` (SoapySDR device
    string, for example `driver=rtlsdr` or `driver=uhd,serial=...`), and options
    `rf_channel`, `center_hz`, `sample_rate_hz` (default: chosen to cover the monitors),
    `bandwidth_hz`, `gain_db` (default: automatic gain if the device has it),
    `antenna`, `clock_source` and `time_source` (for example `external` or `gpsdo`),
    `time_sync` (`pps`, `host` or `device`; use `pps` with a GPSDO), `lock_sensor` (the
    device sensor that says the reference is locked, for example `ref_locked`),
    `buffer_s` (4). Without the bindings every channel fails when it opens:

    ```bash
    petal-locate node --server ws://127.0.0.1:18731 --site manila --driver soapy --soapy-device driver=rtlsdr --driver-option time_sync=pps
    ```

    ```
    ... ERROR   petal_locate.node.agent: driver failed to open: driver: the SoapySDR Python bindings are not installed (system package python3-soapysdr, or the 'sdr' extra of petal-locate); the soapy driver cannot open (No module named 'SoapySDR') ... channel_id=manila-a
    ```
  - `sigmf` (replay a recording as a radio, with its timestamps): `--sigmf-path` names
    the `.sigmf-meta` (or `.sigmf-data`) file; `cf32_le` only; options `loop`, `t0_ns`,
    `realtime`, `clock_locked`. One recording serves every channel of the node, so a
    channel outside its band fails every frame:

    ```bash
    petal-locate node --server ws://127.0.0.1:18731 --site manila --driver sigmf --sigmf-path ./rec-267.sigmf-meta --driver-option loop=true --clock free --clock-unlocked --state-path ./manila-node.json
    ```

    ```
    ... INFO    petal_locate.node.drivers.sigmf: sigmf recording open channel_id=manila-a ... sample_rate_hz=62500.0 center_hz=267400000.0 ... loop=True
    ... WARNING petal_locate.node.monitor: monitor frame failed: out_of_range: requested band 259984375.000..260015625.000 Hz (31250 Hz at 260000000.000 Hz) is outside the available band 267368750.000..267431250.000 Hz channel_id=manila-b ...
    ```
  - `noise` (unit noise, for checking the plumbing) and `sim` (simulated radios; only
    against a server started with `--scenario`, as the demo does).
- **Clock flags.** `--clock ntp|gpsdo|free|sim` and `--clock-unlocked` set what the node
  reports; they do not discipline anything (section 2).
- Run the node under a service manager that restarts it; it reconnects by itself after
  network loss or a server restart, with backoff from 1 s to 30 s.

## 7. Using the console

Open the server's URL (`http://127.0.0.1:18731/` above, or the proxy's). When the server
has an API token the console asks for it. Screens:

- **Watch**: the active events (verdict, frequencies, satellites, position and 95 %
  ellipse) and the map (sites, sub-satellite points, fixes, footprints). Selecting an
  event opens its dossier: the advisory (what to do, in plain words), the evidence
  matrix (each site's channel to each satellite: seen, would see but does not, could not
  see, stale), the position with its ellipse, CEP50, dominant error source, fit and any
  second candidate, the lines of position, the measurements (value, sigma, calibration
  correction and its reference, SNR, residual, and why a row is not used), the
  correlation surface and its cuts, the transitions, and what capture tasking is doing.
- **Spectrum**: live spectrum and waterfall of any monitor window, with the learned
  baseline and detections (authorised carriers flagged).
- **Network**: every site's node, its clock (source, lock, offset), latency and channels.
- **Plan**: accuracy maps for chosen channel pairs and an assumed emitter (with a
  what-if uncertainty that does not change the configuration), and the elevation of
  every satellite from each site.
- **Configure**: the configuration with field-level validation, warnings, save with
  version check, YAML export and import.
- **Simulation** (only when the server simulates): presets, the scenario, emitters
  placed on the map with their link budgets, and truth, labelled simulated.

The verdict means: **uplink** — the interference enters on the satellite uplink and
reaches everyone using that transponder (change channel, transponder or satellite;
moving the terminal will not help); **local** — it is at one site only (relocate,
shield or filter there); **undetermined** — the evidence is not enough yet, and the
dossier says what is missing.

## 8. Calibration and reference transmitters

Calibration is what makes fixes accurate: it measures, through the same two channels, a
transmitter at a known position, and subtracts what that measurement shows of satellite
ephemeris, oscillator and site-clock errors. In the simulator it took the demo jammer's
median miss from 1.33 km to 0.08 km, and with satellites 6 to 8 km off from 25.2 km to
0.10 km (RESULTS.md section 5).

- A reference calibrates only pairs whose two transponders both relay it, so put one in
  every channel you want to locate in, and make sure every satellite relays it at full
  power: the example's references are 8 kHz of noise-like modulation at 20 dBW from an
  omnidirectional antenna, parked at the low edge of a shared 25 kHz channel. A beam aimed
  at one satellite reached the others through its back and made calibrated fixes worse
  than uncalibrated ones. More power soon hurts: at 40 dBW the reference saturated the
  transponder (YAML comments of `config/pacific-uhf.yaml`).
- Survey the reference antenna's phase centre to metres; its error goes straight into
  every calibrated fix.
- Calibration cancels what the target and the reference have in common, so it helps
  less with distance (with TLE-grade ephemeris, a predicted 95 % semi-major of 1.2 km
  near the reference and 4 to 24 km about 3,000 km away). A reference within about
  1,300 km of every area of interest kept it to 1 to 6.5 km (RESULTS.md section 2.2).
  Tasking picks the eligible reference nearest in frequency, not nearest in distance.
- A reference whose band overlaps the interference is not used for that event, so a
  jammer covering the reference gets no calibration.
- Transmitting a reference carrier needs the satellite operator's authorisation like
  any other carrier; list it in `carriers` so it is never raised as interference.
- Settings (`processing.calibration`): `enabled` (true), `interval_s` (30: a pair is
  re-calibrated this often, only while an event uses it), `max_age_s` (300). A
  measurement row shows the correction and the reference it came from.

## 9. The demo

`petal-locate demo` runs the server with `config/pacific-uhf.yaml` and the
`pacific-demo` scenario, and one simulated node process per site over real WebSockets.
Everything it shows is simulated:

```bash
petal-locate demo --port 0
```

```
PETAL Locate demonstration (SIMULATED data)
  Console:   http://127.0.0.1:44309/
  Marking:   UNCLASSIFIED // DEMONSTRATION // SIMULATED DATA
  Config:    /home/user/petal-dish/locate/config/pacific-uhf.yaml
  Scenario:  /home/user/petal-dish/locate/config/scenarios/pacific-demo.yaml (simulated)
  Data:      /tmp/petal-locate-demo-_74duye_ (removed on exit)
  Nodes:     7 (sim driver): honolulu, hilo, tokyo, naha, seoul, busan, manila
Press Ctrl-C to stop.
All 7 nodes are online.
```

In that run (server log) the nodes were online 1.3 s after the server started, the
event was raised 2.8 s later, judged uplink 5.0 s after that and located 7.3 s after it
was raised; the console API then showed `EV-000001 located uplink`, fix 19.9995 N
131.9998 E, 95 % semi-major 423 m, 104 m from the simulated truth, inside the ellipse.
Presets are loaded from the Simulation screen or the API:

```bash
curl -s -X POST http://127.0.0.1:44309/api/v1/sim/presets/local-interferer/load
```

after which the same event turned `local` (`Simulation: Local interference at Busan
(city centre)`). The presets are listed in `locate/README.md`. `--port 0` picks a free
port; `--data-dir` keeps the store; `--driver noise` checks the plumbing without the
simulator. Ctrl-C (SIGINT) stops the server and every node; the log ends with the
event-loop lag statistics (`worst 66 ms, 1 of 865 probes over 50 ms` in that run).

## 10. Accuracy maps and Monte Carlo

**Accuracy map** (MATH.md section 10): the predicted 95 % error, CEP50 and dominant error
source over a grid for given channel pairs and an assumed emitter, without simulating
IQ. The Plan screen draws it; the command line writes JSON:

```bash
petal-locate accuracy --config my-network.yaml --pair tokyo-a,tokyo-b --f-e-hz 301.005e6 --eirp-dbw 20 --bandwidth-hz 10e3 --step-deg 2 --time 2026-01-01T03:00:00Z --reference-carrier ref-guam --out acc.json
```

It took 1.9 s for 2116 cells and gave a value in 2041 of them, the best 1.55 km (one
pair, one TDOA and one FDOA, satellite B with TLE-grade sigmas). `--request FILE` takes
the API's request document instead of flags (an `uncertainty` block there is a what-if).

**Monte Carlo** (simulated): the full chain, simulated IQ through the correlator and the
solver, many times with independent noise, against a configuration and a scenario:

```bash
petal-locate montecarlo --config config/pacific-uhf.yaml --scenario config/scenarios/pacific-demo.yaml --trials 4 --seed 1 --out mc.json
petal-locate montecarlo --config config/pacific-uhf.yaml --scenario config/scenarios/pacific-demo.yaml --trials 2 --seed 1 --draw-errors --param use_calibration=false
```

The first (calibrated, the scenario's own truth errors) took 3.5 s: 4 of 4 solved,
median miss 109 m, mean NEES 1.90 with the total covariance (2 expected). The second
(errors drawn from the configured uncertainties, no calibration) gave a median miss of
1.8 km. `--param` passes any other `run_trials` argument (`capture_s`, `fs`, `t_ns`,
`pairs`, `reference_carrier_id`); `--out` keeps every trial. Use a few hundred trials
for statistics; each takes about 1 s.

## 11. Troubleshooting

- **Node offline** (Network screen, `online: false`). Read the node's log: `bad_token`
  (token wrong, or the server has none for the site; check `--node-tokens` or the
  server's `PETAL_NODE_TOKEN_<SITE_ID>`), `unknown_site` (the site id is not in the
  configuration), `duplicate_node` (another node holds the site), `protocol_mismatch`
  (version skew; the node stops). The server logs every rejected hello with its reason.
  A node silent for three heartbeat periods (6 s) is closed with 4002: check the uplink
  (section 2); on a very slow link lower the capture chunk size (a `NodeAgent` option,
  `chunk_payload_bytes`, not a command-line flag).
- **Channel failed in the ack** (`channels_failed` in the node log, `ok: false` on the
  Network screen): the driver did not open (missing SoapySDR, device busy, out of
  range). A channel whose monitors fall outside its radio's range fails each frame with
  `out_of_range`.
- **Clock unlocked.** Rows read `clock_unlocked` and pairs across sites give FDOA only.
  Check the GPSDO's antenna and lock; captures within one site still give TDOA.
- **No detections.** Run `petal-locate check` at the time in question: is the satellite
  above the site's horizon, does a monitor window cover the downlink (`Monitor
  windows`), is the signal detectable there (the `detectable` column, or
  `POST /api/v1/analysis/link-budget` for an assumed emitter)? The
  tracker learns its baseline over `baseline_tau_s` (600 s) and uses a start-up prior for
  the first tenth of it; a signal that was already on when the tracker started, rose
  slowly, or sits within a few hundred hertz of an authorised carrier may not be raised
  (LIMITATIONS.md section 6). A configuration change rebuilds the trackers it touches.
- **No position.** The dossier's tasking status says why: `fewer than two accepted
  measurements` (pairs still `unconfirmed`: the first detection of each pair waits for a
  repeat, one round of 5 s), `unobservable` (for example a CW, FDOA only, without
  calibration), `ambiguous` correlations (a periodically swept jammer), `narrowband`
  (no TDOA), or no second satellite with a usable channel relays the frequency (`idle`).
- **Ambiguous fixes** (two candidates drawn, "unresolved"). Uncalibrated two-pair
  geometry crosses twice at some times of day; calibration removed it in every simulated
  case. Otherwise more pairs or rows over time, or a tighter `processing.region`, decide
  it. With satellites on the equator the north-south mirror can only be resolved by the
  region.
- **Event judged local.** The signal was seen only near one site and a distant site
  that should have seen it did not. Check the evidence matrix: if the distant channel is
  really blind (antenna pointing, a wrong antenna model or noise temperature makes its
  predicted margin optimistic), correct the configuration. An uplink event whose signal
  stops while a local one takes its frequency turns local after 15 s and its old
  position is set aside; an event detected again after more than 30 s starts afresh.
- **Event stays undetermined.** No sighting at two sites more than 200 km apart, no
  confirmed cross-satellite correlation yet, and no capable distant site to call it
  local: add sites or channels, or wait for tasking.
- **Fix outside its ellipse while the fit looks good** (simulation truth, or a known
  test transmitter): suspect a site clock or a satellite position error larger than
  configured; enable calibration, check the GPSDO, raise the ephemeris sigmas.
