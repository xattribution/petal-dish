# PETAL Locate — deploying and running it

How to set up sites, install the software, write a configuration, run the server and
the nodes, and use the console, calibration, the demo and the planning tools. Read
`LIMITATIONS.md` first: nothing here has run against real satellites or radios, the
`soapy` driver is untested on hardware, and no disciplined-clock implementation is
shipped. Every command below was run as shown (on Linux, Python 3.13) while this
document was written; the outputs quoted are from those runs. Example values (tokens,
ports, file names, the two-site network, the recordings) are illustrative.

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
| 0.72 m PETAL dish | 0.63 | 3.3 to 3.5 dBi, beam about 110 degrees | 10.7 to 11.4 dBW |
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
predicts within about 20 % of the seven-site network.

**Radio (SDR).** One receive stream per channel (the `soapy` driver opens one stream per
channel; a site with one radio per channel gives each channel its own device arguments
with `--channel-option` or a node file, section 6). Requirements:
tuning over the downlink band; a complex sample rate of at least the largest monitor
span plus margin (the example's monitors are 31.25 kHz, captures 20 kHz, and
`max_sample_rate_hz` defaults to 2 MHz); continuous streaming without overflow;
**hardware timestamps** latched to UTC on a PPS edge; an external 10 MHz reference input;
SoapySDR support. The `soapy` driver keeps a 4 s lookback ring (`buffer_s`) and needs the
SoapySDR Python bindings (the system package `python3-soapysdr`, or SoapySDR built from
source; they are not on PyPI).

**Time: a GPS-disciplined oscillator with PPS.** Required for TDOA between sites. A
site-clock error enters every cross-site TDOA one for one (1 us is 300 m of range
difference); the configuration's `clock.sigma_ns` is 30 ns. In the simulator a 10 us
error at one site moved the uncalibrated fix about 31 km (median), mostly outside its
own error ellipse, with no warning from the fit (scenario `clock-fault`, RESULTS.md
section 5). NTP is not enough: it disciplines
the host's clock, not the radio's sample counter, and the samples are timed by the radio.
The radio must take its sample clock from the GPSDO's 10 MHz (FDOA needs the site
oscillator to about 1e-11, which is 2.6 mHz at 260 MHz against FDOA sigmas of about
1 mHz) and set its time on the GPSDO's PPS (`--driver-option time_sync=pps`). The node
must report the reference's lock state honestly: the shipped `SystemClock` reports
`ntp`, locked, without checking anything (simulated nodes report `sim`), and
`--clock gpsdo` only relabels it. A real
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
heartbeat periods, or the server takes it for silent; on a slower uplink lower
`--chunk-payload-bytes` (and set `--send-buffer-bytes`, section 6).

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

pip installs the dependencies named in `pyproject.toml` (numpy, scipy, sgp4, contourpy,
pydantic, PyYAML, FastAPI, uvicorn, websockets 15 or later, which the node needs for its
proxy support, and httpx); only the versions of the development environment were tested
(LIMITATIONS.md section 1). The wheel carries the console build, a copy of `config/`
(example configurations and scenarios, under `petal_locate/data/config/`) and
`THIRD_PARTY.md` (the licenses of the console's bundled fonts and libraries, in the
wheel's `dist-info/licenses/`), so `petal-locate demo` works from an installed package.
For development use the editable install of `CONTRIBUTING.md`. The SoapySDR bindings are
not on PyPI; install them from the system package manager (`python3-soapysdr`) or build
SoapySDR from source. Building the console (Node.js) is not needed: the build is
committed under `src/petal_locate/server/static/`.

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
    name: Tokyo (city center)
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
    name: Manila (city center)
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
    name: Reference carrier, Guam city center (illustrative)
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
  others operator-grade (0.1 / 1 / 1 km); `ephemeris_sigma` overrides either. Those TLE
  sigmas assume fresh elements: `check` warns when a TLE epoch is more than 14 days
  from the time checked (3 days for an orbit faster than 2 revolutions a day). Set
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
  watched satellite (1.25 times the transponder width, at most `max_sample_rate_hz`,
  1024 bins, 8 averages, 2 frames per second; `check` marks them `derived`), or list
  them explicitly (downlink frequencies; `navg` of at least 8 keeps the detection
  threshold calibrated). `check` warns when a channel's windows cover less than 90 % of
  a watched transponder's downlink (it names the blind bands; a transponder wider than
  `max_sample_rate_hz` needs explicit windows that tile it) and when a channel's windows
  spread wider than one tuning of `max_sample_rate_hz` can sample.
- **Carriers.** Every authorised carrier of the frequency plan (so it is not raised as
  interference); a carrier with `location`, `emitter` and `use_as_reference: true` is a
  reference transmitter (section 8).
- **Processing.** Defaults suit the example (ARCHITECTURE.md section 3); set `region`
  to where emitters can be (it resolves mirror ambiguities and bounds searches). The
  solver carries the ionosphere and the troposphere as uncertainty of a stated size,
  `processing.uncertainty.iono_vtec_tecu` (30 TECU) and `tropo_zenith_m` (2.4 m), with
  their correlations between ground ends (`iono_correlation` 0.5, `tropo_correlation`
  0.9); no delay is predicted, so set the ionosphere to the level of the region and
  season (a low-latitude day near solar maximum can exceed 60 TECU; RESULTS.md section
  2.4), and 0 turns a medium off. `processing.solver.max_reduced_chi2` (5) is the fit
  consistency limit of section 7.

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
site 'tokyo'`). Checked at a later time, the same file is warned about its TLE, and a
channel whose radio is limited to 20 kHz (`max_sample_rate_hz: 20.0e+3` added to
`manila-b`) about the part of its 25 kHz transponder its derived window misses:

```bash
petal-locate check --config my-network.yaml --time 2026-11-13T03:00:00Z
```

```
Warnings (2)
  satellites.1.orbit: satellite 'sat-b': its TLE epoch (2026-01-01T00:00:00Z) is 316 days before the time evaluated; elements of a high orbit (1.00 rev/day) more than 14 days old can be far off, while the configured TLE ephemeris sigmas assume fresh elements; load current elements
  ...
  sites.1.channels.1.monitors: channel 'manila-b': its monitor windows cover 80 % of the downlink of transponder 'ch-1' of satellite 'sat-b' (derived windows are at most max_sample_rate_hz = 0.02 MHz wide); nothing in 259.988 to 259.990 MHz, 260.010 to 260.012 MHz can be detected through this channel, and a wider signal is measured over the window only; add monitors that tile the passband
```

(the last line from the 20 kHz variant). A satellite whose orbit cannot be evaluated at
`--time` (an ephemeris table that has ended) is reported (`satellite X: orbit not
available at this time; its paths are left out`) and does not stop the check. Add
`--scenario FILE` to check a simulation scenario against the configuration too.

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
... INFO    petal_locate.cli: serving 'my-network' on http://127.0.0.1:18731/ (not simulating) api_token=True marking=True port=18731
... INFO    petal_locate.server.store: store opened at data/petal-locate.sqlite3 (run ..., previous run None) ...
... INFO    petal_locate.server.app: server started version=0.1.0 runtime=True
```

- **Settings.** `--host` (default 127.0.0.1) and `--port` (default 8080; `--port 0`
  takes a free port, and the `serving` line names the port really bound; a port in use
  exits 2); `--data-dir` (default `./data`); `--node-tokens`; a node token can also come
  from the server's environment as `PETAL_NODE_TOKEN_<SITE_ID>` (site id upper-cased,
  other characters as `_`). A site with no token cannot connect: the server warns at
  start-up (`no node token for site(s) ...: their nodes cannot connect`), and the
  Configure screen, `GET /api/v1/config` and `POST /api/v1/config/validate` warn about
  every site without one. The tokens file is read again whenever it changes (`node
  tokens file reloaded: 3 site(s)` in the log), so a site added through the console
  gets its token by editing the file, without a restart; a file that no longer loads is
  logged and its last good content kept. The API token comes from `--api-token` or,
  better (it stays out of the process list), `PETAL_API_TOKEN`; without one the server
  logs a warning (`no API token ...: the console interface is open to anything that can
  reach 127.0.0.1`). Without `--scenario` the server does not simulate. `--log-format
  json` writes one JSON object per line; `--access-log` logs every HTTP request. Exit
  code 2 means the configuration or a flag is wrong, 1 that the server did not start.
  SIGINT or SIGTERM stops it cleanly (exit 0; nodes see close code 1001).
- **Browser access.** The console routes answer only requests whose `Host` names the
  bound address, `localhost`, `127.0.0.1` or `::1` (plus this machine's names and
  addresses when bound to `0.0.0.0` or `::`); any other name gets 400 (`the host name
  'x' is not one this server answers to; start it with --allowed-host NAME to allow
  it`), which stops a web page from re-pointing its own name at the server. Give every
  other name operators use (the proxy's public name) with `--allowed-host NAME`
  (repeatable; `*` allows any). The console socket refuses a browser page of another
  origin; `--allowed-origin https://name` admits one. A state-changing API request
  (POST, PUT, DELETE) must carry `Content-Type: application/json` or
  `application/yaml`, an `Authorization` header, or `X-Petal-Client` (403 otherwise),
  and every API body is limited to 4 MiB (413).
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
{"version":"0.1.0","marking":{"level":"UNCLASSIFIED","line":"UNCLASSIFIED // TEST"},"simulation":false,"server_t_ns":"1794608306304280131","config_name":"my-network","api_token_required":true}
{"server_t_ns":"1794608306314802127","config_version":1,"nodes":[{"site_id":"tokyo","node_id":"b26c4fd3-...","online":true,"latency_ms":3.78,"clock":{"source":"ntp","locked":true,"est_offset_ns":0.0,"holdover_s":0.0},"driver":"noise", ...
{"status":"ok","version":"0.1.0","nodes_online":2}
```

  A configuration document sent with curl needs its type, since curl's default
  (`application/x-www-form-urlencoded`) is refused: `curl -X POST -H "Authorization:
  Bearer $(cat api-token.txt)" -H "Content-Type: application/yaml" --data-binary
  @my-network.yaml http://127.0.0.1:18731/api/v1/config/validate`.
- **TLS.** The server speaks plain HTTP and WebSocket. In production bind it to
  127.0.0.1 and put a TLS-terminating reverse proxy in front of it. The proxy must pass
  WebSocket upgrades on `/ws/node` and `/ws/ui`, allow messages of 1 MiB, not buffer
  responses (spectrum and event streams), keep idle WebSockets open longer than the 20 s
  keepalive, cap connections per client (the server cannot abort a half-open
  connection), and serve `/`, `/api/`, `/node/v1/sim` and `/healthz`. It should pass the
  `Host` header and `X-Forwarded-Proto` (uvicorn trusts forwarded headers from
  127.0.0.1), and the server needs `--allowed-host <public name>`; without
  `X-Forwarded-Proto` the console socket also needs `--allowed-origin
  https://<public name>`. Without TLS the node tokens and the API token travel in clear
  text. No proxy configuration was tested for this document.
- **Data directory and retention.** `--data-dir` holds `petal-locate.sqlite3` (WAL
  mode; configurations, scenarios, events with their whole history, measurements,
  solutions, calibrations, node sessions) and `captures/` (every completed IQ capture as
  a SigMF pair). Captures are kept up to `--capture-limit-bytes` in total (default
  536870912, 512 MiB); the oldest are deleted beyond it. Event history is kept for ever
  unless `--history-max-age-s S` (delete events not updated for S seconds, with their
  transitions, evidence, measurements and solutions; calibrations and closed node
  sessions older than S go too) or `--history-max-events N` (keep the N most recently
  updated events) is given; the store applies them when it opens and then at most once a
  minute while events are written. For example, a server started on the store of an
  earlier run with `--history-max-age-s 3600 --history-max-events 100` logged `history
  retention: deleted 1 event(s), 8 calibration(s), 7 node session(s)` as it opened.
  Restarting on the same directory resumes it: the newest configuration saved through
  the console wins over the `--config` file (the log says so), and earlier events
  appear as inactive history. Back up or rotate the directory with the server stopped.
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
  (YAML scalars) for every channel, and `--channel-option CHANNEL:KEY=VALUE` (repeatable)
  for one channel on top of those, for example one radio per channel. Every channel's
  options are checked before the node dials out (unknown options exit 2, naming the
  channel: `petal-locate node: --driver sigmf (channel manila-a): options: driver
  'sigmf' refused its options (... unexpected keyword argument 'bogus')`). The options
  every channel gets must be complete on their own (a `path` or `device` the driver
  requires cannot be given per channel only).
  - `soapy` (real radios; untested on hardware): `--soapy-device ARGS` (SoapySDR device
    string, for example `driver=rtlsdr` or `driver=uhd,serial=...`), and options
    `rf_channel`, `center_hz`, `sample_rate_hz` (default: chosen to cover the monitors),
    `bandwidth_hz`, `gain_db` (default: automatic gain if the device has it),
    `antenna`, `clock_source` and `time_source` (for example `external` or `gpsdo`),
    `time_sync` (`pps`, `host` or `device`; use `pps` with a GPSDO), `lock_sensor` (the
    device sensor that says the reference is locked, for example `ref_locked`),
    `buffer_s` (4). With one radio per channel, give each its device string. Without the
    bindings every channel fails when it opens:

    ```bash
    petal-locate node --server ws://127.0.0.1:18731 --site manila --driver soapy --driver-option time_sync=pps --channel-option manila-a:device=driver=rtlsdr,serial=00000001 --channel-option manila-b:device=driver=rtlsdr,serial=00000002
    ```

    ```
    ... ERROR   petal_locate.node.agent: driver failed to open: driver: the SoapySDR Python bindings are not installed (system package python3-soapysdr, or SoapySDR built from source; they are not on PyPI); the soapy driver cannot open (No module named 'SoapySDR') ... channel_id=manila-b
    ... ERROR   petal_locate.node.agent: driver failed to open: driver: the SoapySDR Python bindings are not installed ... channel_id=manila-a
    ... INFO    petal_locate.node.agent: assignment applied ... channels_ok=[] channels_failed="['manila-a', 'manila-b']"
    ```
  - `sigmf` (replay a recording as a radio, with its timestamps): `--sigmf-path` names
    the `.sigmf-meta` (or `.sigmf-data`) file; `cf32_le` only; options `loop`, `t0_ns`,
    `realtime`, `clock_locked`. One recording serves every channel that has no `path`
    of its own, and a channel outside its recording's band fails every frame
    (`monitor frame failed: out_of_range: requested band ... is outside the available
    band ...`). A node file gives each channel its own recording:

    ```yaml
    # manila-node.yaml: one recording per channel.
    driver_options: {path: ./rec-267.sigmf-meta, loop: true}   # every channel
    channels:
      manila-b: {path: ./rec-260.sigmf-meta}                   # this channel's own recording
    ```

    ```bash
    petal-locate node --server ws://127.0.0.1:18731 --site manila --driver sigmf --node-config manila-node.yaml --clock free --clock-unlocked --state-path ./manila-node.json
    ```

    ```
    ... INFO    petal_locate.node.drivers.sigmf: sigmf recording open channel_id=manila-b meta_path=rec-260.sigmf-meta sample_rate_hz=62500.0 center_hz=260000000.0 ... loop=True
    ... INFO    petal_locate.node.drivers.sigmf: sigmf recording open channel_id=manila-a meta_path=rec-267.sigmf-meta sample_rate_hz=62500.0 center_hz=267400000.0 ... loop=True
    ... INFO    petal_locate.node.agent: assignment applied ... channels_ok="['manila-a', 'manila-b']" channels_failed=[]
    ```
  - `noise` (unit noise, for checking the plumbing) and `sim` (simulated radios; only
    against a server started with `--scenario`, as the demo does).
- **Node file.** `--node-config FILE` is YAML with the keys `driver_options` (every
  channel), `channels` (channel id to that channel's options), `chunk_payload_bytes`
  and `send_buffer_bytes`; any other key exits 2. Flags win over the file.
- **Slow uplinks.** `--chunk-payload-bytes N` (8 to 262144, default 32768) is the sample
  payload of one capture chunk: lower it so that a chunk crosses the uplink well within
  three heartbeat periods (6 s). `--send-buffer-bytes N` sets the socket send buffer
  (for example 262144), which bounds how long control messages wait behind samples
  already handed to the kernel.
- **Clock flags.** `--clock ntp|gpsdo|free|sim` and `--clock-unlocked` set what the node
  reports; they do not discipline anything (section 2). The default is `sim` for
  `--driver sim` and `ntp` otherwise. `--sim-time-offset-ns` (only with `--driver sim`)
  moves a simulated node's clock as `petal-locate demo --start` does (section 9).
- Run the node under a service manager that restarts it; it reconnects by itself after
  network loss or a server restart, with backoff from 1 s to 30 s.

## 7. Using the console

Open the server's URL (`http://127.0.0.1:18731/` above, or the proxy's). When the server
has an API token the console asks for it once per browser tab; an address that carries
it (`/?token=<token>`, as the demo prints) hands it to the console and removes it from
the address bar. The header roll-up shows the worst state of nodes, satellites and
events: `No data` until the first status arrives, and `not current: status N old` with
the last level while the link is down (the queue and dossier then say `as of HH:MM:SSZ`
instead of `now`). A screen that cannot load shows why with a Retry button and is
retried when the link comes back. Screens:

- **Watch**: the active events (verdict, frequencies, satellites, position and 95 %
  semi-axes) and the map (sites, sub-satellite points, fixes, footprints). A local
  event shows the frequency it is received at and the sites that see it. Satellites
  whose ephemeris the server cannot evaluate are listed as Serious with the server's
  message. Selecting an event opens its dossier: the start time to report (the onset of
  the current transmission), `Detected again ... after a break of N` after a loss,
  satellites not affected (they cannot see the estimated position), the advisory (what
  to do, in plain words), the evidence matrix (each site's channel to each satellite:
  seen, would see but does not, could not see, stale), the position with its ellipse,
  CEP50, dominant error source (`I` ionosphere and `T` troposphere among them), fit and
  any second candidate, the lines of position, the measurements (value, sigma,
  calibration correction and its reference, SNR, residual, and why a row is not used),
  the correlation surface and its cuts, the transitions, what capture tasking is doing,
  and the ellipse against time (rows set aside after a break or another transmitter are
  grayed, and a dashed rule marks where the current transmission starts). Cautions:
  `Not a fix` for a fit whose measurements do not agree (its ellipse is drawn dotted and
  enlarged, and it is never reported as located), `One of N positions` with every
  candidate when the system cannot choose between them, `Re-check` while a position
  from before a break or a change of signal is checked against new measurements, and
  `Cannot be located` with the reason when no position can be computed.
- **Spectrum**: live spectrum and waterfall of any monitor window, with the learned
  baseline and detections (authorized carriers flagged); the header marks a window whose
  detection is blind or whose noise floor is raised.
- **Network**: every site's node, its clock (source, lock, offset), its clock offset
  estimated from the reference calibrations (`By calibration`, with a Caution when a
  site is 1 us or more off the sites it is compared with, more when the configured clock
  or ephemeris sigmas are large), latency and channels (a channel names a
  window that receives frames but accepts none, `detection blind on <window>`, or whose
  noise floor is raised), and a Satellites table (ephemeris available, or since when
  not).
- **Plan**: accuracy maps for chosen channel pairs and an assumed emitter (with a
  what-if uncertainty that does not change the configuration), and the elevation of
  every satellite from each site. The form starts from the loaded configuration (the
  uplink band most satellites share, pairs at the site that watches most of them).
- **Configure**: the configuration with field-level validation, warnings (including
  sites with no node token on the server), save with version check, YAML export and
  import.
- **Simulation** (only when the server simulates): presets, the scenario, emitters
  placed on the map with their link budgets, truth errors (ionosphere and troposphere
  under Propagation media), and truth, labelled simulated. The clock strip says
  `simulated time, N behind this computer` when the demo runs from a fixed start.

The verdict means: **uplink** — the interference enters on the satellite uplink and
reaches everyone using that transponder (change channel, transponder or satellite;
moving the terminal will not help); **local** — it is at one site only (relocate,
shield or filter there); **undetermined** — the evidence is not enough yet, and the
dossier says what is missing. An event is **located** only when its measurements fit
one stationary transmitter (reduced chi-square at most
`processing.solver.max_reduced_chi2`, 5, or a tail probability above 0.001); otherwise
it stays `locating` with the `Not a fix` caution.

## 8. Calibration and reference transmitters

Calibration is what makes fixes accurate: it measures, through the same two channels, a
transmitter at a known position, and subtracts what that measurement shows of satellite
ephemeris, oscillator and site-clock errors, and of the ionosphere and troposphere on
the downlinks (the part of the uplinks the jammer and the reference do not share
remains). In the simulator it took the demo jammer's median miss from 1.33 km to
0.13 km, and with satellites 6 to 8 km off from 25.2 km to 0.13 km (RESULTS.md
section 5).

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
  near the reference and 4.4 to 24 km about 3,000 km away) and depends on direction. A
  reference 650 km from an area of interest, in any direction from which it still sees
  every satellite the pairs use, kept the TLE-grade semi-major to 1.2 to 4.9 km; at
  1,300 km it was 2.3 to 3.3 km due east or west but 6.8 to 10 km due north or south
  (RESULTS.md section 2.5). A reference that cannot see one of the satellites (MUOS
  Indo-Asia is below the horizon east of about 160 E) leaves that satellite's pairs
  uncalibrated. With several eligible references, tasking uses the one nearest the
  event's current fix (nearest in frequency before there is a fix).
- A reference whose band overlaps the interference is not used for that event, so a
  jammer covering the reference gets no calibration.
- Transmitting a reference carrier needs the satellite operator's authorization like
  any other carrier; list it in `carriers` so it is never raised as interference.
- Settings (`processing.calibration`): `enabled` (true), `interval_s` (30: a pair is
  re-calibrated this often, only while an event uses it), `max_age_s` (300). A
  measurement row shows the correction and the reference it came from.
- The calibrations also give each site's clock offset (`/status` `site_clocks`, the
  Network screen's `By calibration` column). With the `clock-fault` scenario's
  calibrations the fit put Naha at -10.05 us and flagged it; in a live run where every
  calibration involved Naha's 172 E channel, the offset went to that satellite instead
  and nothing was flagged. A site joined through one satellite combination only cannot
  be told apart from those satellites (LIMITATIONS.md section 7), so a clean column is
  no proof of good clocks.

## 9. The demo

`petal-locate demo` runs the server with `config/pacific-uhf.yaml` and the
`pacific-demo` scenario, and one simulated node process per site over real WebSockets.
Everything it shows is simulated:

```bash
petal-locate demo --port 0
```

```
PETAL Locate demonstration (SIMULATED data)
  Console:    http://127.0.0.1:41403/?token=QIgXBCytF0N6RFmn0yjXficoCAi10HRQ
  API token:  QIgXBCytF0N6RFmn0yjXficoCAi10HRQ (the address above carries it; enter it if the console asks)
  Marking:    UNCLASSIFIED
  Config:     /home/user/petal-dish/locate/config/pacific-uhf.yaml
  Scenario:   /home/user/petal-dish/locate/config/scenarios/pacific-demo.yaml (simulated)
  Data:       /tmp/petal-locate-demo-cwvwwbcj (removed on exit)
  Nodes:      7 (sim driver): honolulu, hilo, tokyo, naha, seoul, busan, manila
Press Ctrl-C to stop.
All 7 nodes are online.
```

Each run makes a random API token (and random node tokens). Open the printed console
address: it carries the token, which the console keeps for that browser tab and takes
out of the address bar. The API needs it as a bearer token. In that run (server log) the
nodes were online 0.6 s after the server started, the event was raised 3.4 s after the
start, judged uplink 5.0 s after that and located 7.5 s after it was raised; 15 s later
the console API showed `EV-000001 located uplink`, fix 20.0007 N 132.0000 E, 95 %
semi-axes 452 by 244 m, 74 m from the simulated truth, inside the ellipse (the preset
has no ionosphere, but the solver allows for one, so the ellipse is cautious). Presets
are loaded from the Simulation screen or the API:

```bash
curl -s -X POST -H "Authorization: Bearer QIgXBCytF0N6RFmn0yjXficoCAi10HRQ" http://127.0.0.1:41403/api/v1/sim/presets/local-interferer/load
```

after which the same event turned `local` (`Simulation: Local interference at Busan
(city center)`). The presets are listed in `locate/README.md`. `--port 0` picks a free
port; `--data-dir` keeps the store; `--driver noise` checks the plumbing without the
simulator; `--no-api-token` runs without a token (anything that can reach the port can
then drive the demo; the demo warns). Ctrl-C (SIGINT) stops the server and every node;
the log ends with the event-loop lag statistics (`worst 21 ms, 0 of 969 probes over
50 ms` in that run).

The simulated world follows the host clock, so what the satellites do (and when the
uncalibrated fix has two candidates) depends on the time of day. `--start` runs it from
a fixed UTC time instead, so that runs can be repeated:

```bash
petal-locate demo --port 0 --start 2026-11-13T03:00:00Z
```

```
  ...
  Start:      simulated time starts at 2026-11-13T03:00:01Z
Press Ctrl-C to stop.
All 7 nodes are online.
```

The server and every node then run on the host clock plus one fixed offset: the event
in that run was first seen at 03:00:03Z simulated time and located with 95 % semi-axes
482 by 241 m. Log lines keep the host time; times in the API and the console are
simulated. `--start` needs `--driver sim` and a time between 1970 and 2230. The live
end-to-end tests use it.

## 10. Accuracy maps and Monte Carlo

**Accuracy map** (MATH.md section 10): the predicted 95 % error, CEP50 and dominant error
source over a grid for given channel pairs and an assumed emitter, without simulating
IQ. The Plan screen draws it; the command line writes JSON:

```bash
petal-locate accuracy --config my-network.yaml --pair tokyo-a,tokyo-b --f-e-hz 301.005e6 --eirp-dbw 20 --bandwidth-hz 10e3 --step-deg 2 --time 2026-01-01T03:00:00Z --reference-carrier ref-guam --out acc.json
```

It took 2.1 s for 2116 cells and gave a value in 2041 of them, the best 1.55 km (one
pair, one TDOA and one FDOA, satellite B with TLE-grade sigmas; the dominant error is
that satellite's velocity everywhere). `--request FILE` takes the API's request
document instead of flags (an `uncertainty` block there is a what-if; it may set the
ionosphere and troposphere sizes too). The planning code takes a pair end
`channel@satellite` for a fixed-pointing channel that watches several satellites
relaying the frequency, but this command and the API do not accept that form yet
(LIMITATIONS.md section 8).

**Monte Carlo** (simulated): the full chain, simulated IQ through the correlator and the
solver, many times with independent noise, against a configuration and a scenario:

```bash
petal-locate montecarlo --config config/pacific-uhf.yaml --scenario config/scenarios/pacific-demo.yaml --trials 4 --seed 1 --out mc.json
petal-locate montecarlo --config config/pacific-uhf.yaml --scenario config/scenarios/pacific-demo.yaml --trials 2 --seed 1 --draw-errors --param use_calibration=false
```

The first (calibrated, the scenario's own truth errors) took 4.5 s: 4 of 4 solved,
median miss 114 m, mean NEES 0.81 with the total covariance (2 expected; the scenario
has no ionosphere or troposphere while the solver allows for both, so its ellipses are
cautious). The second (errors drawn from the configured uncertainties, an ionosphere
and a troposphere drawn for every trial, no calibration) gave a median miss of 1.8 km.
`--param` passes any other `run_trials` argument (`capture_s`, `fs`, `t_ns`, `pairs`,
`reference_carrier_id`); `--out` keeps every trial. Use a few hundred trials for
statistics; each takes about 1 s.

## 11. Troubleshooting

- **Node offline** (Network screen, `online: false`). Read the node's log: `bad_token`
  (token wrong, or the server has none for the site; check `--node-tokens` or the
  server's `PETAL_NODE_TOKEN_<SITE_ID>`), `unknown_site` (the site id is not in the
  configuration), `duplicate_node` (another node holds the site), `protocol_mismatch`
  (version skew; the node stops). The server logs every rejected hello with its reason.
  A node silent for three heartbeat periods (6 s) is closed with 4002: check the uplink
  (section 2); on a very slow link lower `--chunk-payload-bytes` (section 6).
- **Site has no node token** (warning in the Configure screen, `GET /api/v1/config` or
  the server's start-up log): add the site to the tokens file; the server reads it
  again when it changes.
- **Channel failed in the ack** (`channels_failed` in the node log, `ok: false` on the
  Network screen): the driver did not open (missing SoapySDR, device busy, out of
  range). A channel whose monitors fall outside its radio's range fails each frame with
  `out_of_range`.
- **Detection blind** (`detection blind on <window>` on the Network screen, `ok:
  false`; server log `window X/Y: detection is blind, no frame accepted for 10 frame
  periods while frames arrive (...)`): frames reach the server but none is accepted,
  for example frames whose span or rate differs from the assignment. Frames stamped
  more than 60 s ahead of the server clock are dropped (`site 'x' sends spectrum frames
  stamped N s ahead of the server clock ...`, once per session): fix the node's clock.
  A node clock that steps back by more than three frame periods is taken as a clock
  step (`the node's clock stepped back, its frames are accepted from this one on`);
  check its time reference.
- **Clock unlocked.** Rows read `clock_unlocked` and pairs across sites give FDOA only.
  Check the GPSDO's antenna and lock; captures within one site still give TDOA.
- **No detections.** Run `petal-locate check` at the time in question: is the satellite
  above the site's horizon, does a monitor window cover the downlink (`Monitor
  windows`, and the coverage warnings), is the signal detectable there (the
  `detectable` column, or `POST /api/v1/analysis/link-budget` for an assumed emitter)?
  The tracker learns its baseline over `baseline_tau_s` (600 s) and uses a start-up
  floor for the first tenth of it; a signal already on at start-up is found against
  that floor unless it fills the whole window. A window-filling signal is learned as
  that window's noise; the server then logs `window X/Y: its noise floor is N dB above
  the channel's other windows; a signal filling the window may be taken for its noise`
  and the Network and Spectrum screens show the raised floor (only for a channel with
  other windows of the same span). A signal that rose slowly, or sits within a few
  hundred hertz of an authorized carrier, may not be raised (LIMITATIONS.md section 6).
  A configuration change rebuilds the trackers it touches.
- **No position.** The dossier's tasking status says why: `fewer than two accepted
  measurements` (pairs still `unconfirmed`: the first detection of each pair waits for a
  repeat, one round of 5 s), `unobservable` (for example a CW, FDOA only, without
  calibration), `ambiguous` correlations (a periodically swept jammer), `narrowband`
  (no TDOA), or no second satellite with a usable channel relays the frequency (`idle`).
  When the signal itself cannot be located (it repeats itself, its delay and Doppler
  are coupled, or fewer than two visible satellites relay it) the dossier says so
  under `Cannot be located`, and the advisory says no position can be computed.
  `Satellite X: orbit not available (...); its pairs are not tasked` means that
  satellite's ephemeris does not cover the present (an ephemeris table that ended;
  also on the Watch and Network screens): load a current ephemeris.
- **Not a fix** (Caution in the dossier, `Measurements inconsistent: no fix` in the
  queue). The measurements do not fit one stationary transmitter: two transmitters on
  the frequency, a moving one, or a fault larger than the configured uncertainties.
  The position is shown with its ellipse enlarged by the square root of the reduced
  chi-square and must not be reported.
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
  position is set aside.
- **Event detected again, or the signal changed** (`Re-check` in the dossier). An
  event back within 30 s keeps its position under re-check, and so does one whose
  waveform class, level (6 dB) or width (twice) changes: the next detection that
  agrees keeps it, one that disagrees sets it aside. After a longer break the earlier
  position and measurements are set aside at once. When another transmitter takes the
  frequency without a break, the pairs' measurements jump further than the satellites'
  motion allows and the earlier rows are set aside (`another transmission: ...` in the
  transitions). Report the dossier's start time: it is the onset of the current
  transmission.
- **Event stays undetermined.** No sighting at two sites more than 200 km apart, no
  confirmed cross-satellite correlation yet, and no capable distant site to call it
  local: add sites or channels, or wait for tasking.
- **Fix outside its ellipse while the fit looks good** (simulation truth, or a known
  test transmitter): suspect a site clock or a satellite position error larger than
  configured, or an ionosphere above `iono_vtec_tecu`; enable calibration, look at the
  Network screen's `By calibration` column, check the GPSDO, raise the ephemeris sigmas
  or the ionosphere level.
- **Console refused** (`the host name 'x' is not one this server answers to` from a
  browser or the proxy): start the server with `--allowed-host x`. A console page of
  another origin whose live view stays empty is refused by the console socket (server
  log `console socket refused: origin ... is not allowed`): `--allowed-origin`. A
  script's POST or PUT answered 403 needs `Content-Type: application/json` or
  `application/yaml`, or the `Authorization` header (section 5).
