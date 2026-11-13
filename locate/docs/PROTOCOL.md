# PETAL Locate — wire protocols (version 1)

Two interfaces: nodes to server (section A) and operator console to server
(section B). This document fixes the shapes and the rules both ends follow; the code is
`protocol/messages.py`, `protocol/framing.py`, `server/hub.py` and `node/agent.py`
(section A) and `server/api/schemas.py`, `rest.py`, `ws.py` (section B). For section B
FastAPI's `/openapi.json` is the authoritative schema.

# A. Node to server

Transport: one WebSocket per node, `ws(s)://server/ws/node`. Control messages are text
frames holding one JSON object with a `type` field. Sample and spectrum data are binary
frames. The node always dials out, so sites behind NAT need no inbound ports.
Production deployments terminate TLS in front of the server (`wss`); the token below
is a bearer secret and must not travel in clear text outside a lab (the node logs a
warning when it would send it over `ws://` to a host that is not a loopback address).

All absolute times are integer UTC nanoseconds (`*_ns`), sent as JSON integers on this
interface (both ends are Python), in `0 .. 2^63 - 1`. `t_first_ns` and every `t_ns` are
**labelled** times (the node's clock). The one `*_ns` field that is not an absolute
time, `ClockStatus.est_offset_ns`, is a JSON number.

permessage-deflate is disabled on both ends (`websockets.connect(compression=None)`;
uvicorn `ws_per_message_deflate=False`) and both accept messages of exactly 1 MiB
(`ws_max_size`): IQ does not compress and deflate costs 139 ms of event loop per 4 MiB.
The server must be run through `server.app.run` or `build_server` (as the CLI does):
plain `uvicorn module:app` loses these settings and the log filters that keep tokens
out of the log, and nodes then see close code 1012 instead of 1001 at shutdown.

## Parsing

- Receivers are tolerant: unknown fields are ignored, and unknown `reject.reason` and
  `capture_error.code` values are accepted. Senders are exact: a message built in code
  with an unknown field, reason or code is refused before it is sent.
- Types are strict, with no coercion: an integer field refuses `1.0`, `"1"` and
  `true`; a float field takes any JSON number. `NaN`, `Infinity` and numbers that
  overflow a double are malformed.
- Identifiers are 1 to 256 characters. Duplicate `channel_id` in `welcome`, `assign` or
  `assign_ack`, and a duplicate `monitor_id` within a channel, are malformed.
- A text frame whose `type` is `psd` or `capture_data`, or a binary header whose type is
  a control message, is malformed (not an unknown type).
- Error details name fields and never quote values, so the `hello` token cannot reach an
  `error` reply, a log line or a traceback.

## Binary frame

```
bytes 0..3    magic  b"PLB1"
bytes 4..7    header length H, uint32 little-endian
bytes 8..8+H  header, UTF-8 JSON object with a "type" field
remainder     payload, little-endian
```

Senders pad the header JSON with up to 7 trailing spaces so the payload starts on an
8-byte boundary (`H` includes the padding); receivers accept unpadded frames. Payloads
are `float32` (spectra) or `complex64` (IQ) only and never contain NaN or infinities;
both ends refuse such a payload on encode and on decode.

Receivers reject frames with a wrong magic, an oversized header (> 64 KiB), or a
payload whose length does not match the header.

## Session

1. Node -> `hello`
   `{type, protocol: 1, node_id, site_id, token, software, driver, clock: ClockStatus, t_ns}`
   `node_id` is a UUID the node generates once and persists (or `--node-id`). The
   server waits at most `hello_timeout_s` (10 s) for a valid `hello`, then closes with
   4002. A second `hello`, or a message only a server sends, is malformed.
2. Server -> `welcome`
   `{type, protocol: 1, server_t_ns, heartbeat_s (default 2), config_version,
   channels: [ChannelAssignment], simulation: {enabled, scenario_url?, version?}}`
   or `reject {type, reason}` followed by close (code 1008). Reasons, checked in this
   order: `protocol_mismatch` (answered even when the rest of the `hello` does not
   validate as version 1; any integer `protocol` and an empty token parse for this
   reason), `bad_token` (wrong token, or the server holds no token for the site, so
   only a token holder can learn which sites exist), `unknown_site` (a valid token for
   a site not in the configuration), `duplicate_node` (a *different* node_id holds the
   site). `simulation` may be absent (= not simulating); `scenario_url` and `version`
   are left out when unset.
   A hello whose node_id equals that of the live session for the site **replaces** it:
   the server closes the old socket with code 4001, fails its pending captures and
   continues with `welcome`. Two processes started with the same node_id therefore
   replace each other about every 30 s (the reconnect backoff after a 4001).
3. After `welcome` and after every `assign` the node opens one driver per new or changed
   channel and sends `assign_ack {type, config_version, ok, channels: [{channel_id, ok,
   error?, capabilities?: {min_hz, max_hz, max_sample_rate_hz, lookback_s,
   concurrent_reads}}]}` (`capabilities` is required when `ok` and may be absent for a
   channel whose driver failed to open). A channel whose assignment is unchanged keeps
   its driver, monitors and captures through an `assign` (the driver's optional
   `on_assign` hook runs before the ack; a failing hook marks the channel failed); only
   removed or changed channels are closed and reopened. Drivers are closed at the end of
   every session and reopened after the next `welcome`. An ack counts only for the
   `config_version` last sent (stale acks are ignored and counted), and a node holds one
   pending assignment, newest wins, so an assign superseded before it was applied is
   never acknowledged. After every `assign` all channels of the site are *not ready*
   until the new ack arrives, unchanged ones included (their pending captures go on); a
   channel the ack omits stays not ready. The server does not task a channel that is
   not ready, or accept its spectrum frames; tasking uses
   `min(config max_sample_rate_hz, capabilities.max_sample_rate_hz)`. A node acks only
   after every new driver has opened or failed (at most `driver_open_timeout_s`, 30 s).
4. Node -> `heartbeat {type, seq, t_ns, rtt_ms?, clock: ClockStatus, load: {cpu, queue}}`
   every `heartbeat_s`. `rtt_ms` is the node-measured round trip of the last answered
   heartbeat, omitted (or null) until one has been measured; while a heartbeat is
   unanswered for longer than that, its age (a lower bound) is reported instead. `cpu`
   is the node agent's processor load as a fraction of one core (it may exceed 1);
   `queue` is the number of capture requests waiting or in progress. Server ->
   `heartbeat_ack {type, seq, server_t_ns}`. Any frame received from a node counts as
   liveness. Three heartbeat periods of silence mark the node offline, fail its pending
   captures and close the socket (code 4002), so a half-open connection cannot hold the
   site. A node that hears nothing from the server for `max(10 s, 5 heartbeat_s)` closes
   with 1011 and reconnects. The round trip includes time queued behind sample uploads
   in the node's socket buffer (174 ms on loopback during a 64 MiB upload; seconds on a
   slow uplink).
5. Server -> `assign {type, config_version, channels: [ChannelAssignment]}` whenever
   configuration changes; nothing is sent to a site whose version and channels are
   unchanged. An assign fails pending captures on removed or changed channels (the
   node answers each with `capture_error {code: "cancelled"}`). A site removed from the
   configuration is closed with code 1008 and forgotten.

```
ChannelAssignment  {channel_id, antenna_summary, monitors: [{monitor_id, center_hz, span_hz, nfft, navg, frame_rate_hz}]}
ClockStatus        {source: "gpsdo" | "ntp" | "sim" | "free", locked: bool, est_offset_ns: number, holdover_s: number}
```

`antenna_summary` is display text. Limits: `nfft` is 2 to 131072 (so a spectrum frame
fits one 1 MiB message; the configuration requires at least 16, ARCHITECTURE.md
section 3); a capture is 1 to 8,388,608 samples (64 MiB).

`est_offset_ns` is the node's estimate of `delta_g` of MATH.md section 4 (true time
minus labelled time). It is displayed and never applied to measurements. A capture
whose ClockStatus has `locked = false` yields no TDOA unless both channels of the pair
are at the same site. The shipped `SystemClock` reports `ntp`, locked, without asking
the operating system whether NTP is synchronised; `--clock` and `--clock-unlocked`
override what is reported (OPERATIONS.md).

## Spectrum frames (node -> server, binary)

Header `{type: "psd", channel_id, monitor_id, t_ns, center_hz, span_hz, nfft, navg,
seq, units: "dB"}`; payload `float32[nfft]`, fft-shifted so bin `k` is at
`center_hz + (k - nfft/2) * span_hz / nfft`; `t_ns` is the first-sample time.

PSD frames are droppable: the node keeps at most one unsent frame per monitor (newest
wins), so gaps in `seq` are normal. `center_hz`, `span_hz` and `t_ns` are what the
driver actually measured. `seq` restarts at 0 when a channel is reopened or a session
starts. The server drops (and counts, without treating them as malformed) frames for
channels not acknowledged under the current assignment, windows that are not assigned,
or a different `nfft`; its detection pipeline also drops frames whose centre moved by
more than half a bin, that hold a non-finite bin, or whose `t_ns` repeats or goes back.

## Captures

Server -> `capture_request`
`{type, capture_id, channel_id, t_start_ns, n_samples, sample_rate_hz, center_hz,
deadline_ns}`

`t_start_ns` is normally a little in the future. It may be in the past when the driver
advertises `lookback_s` and still holds that interval. `deadline_ns` is the node-clock
time by which the last chunk must be sent. A second request for a `capture_id` still in
progress is ignored (logged).

Node -> one or more binary frames, header
`{type: "capture_data", capture_id, channel_id, seq, last: bool, t_first_ns,
sample_rate_hz, center_hz, n_samples_total, offset, n, overflow: bool, clock:
ClockStatus}`; payload `complex64[n]` (interleaved float32 I, Q). Chunks are at most
256 KiB of payload (32 KiB by default, so that heartbeats still get through a
44 kbit/s uplink; `NodeAgent(chunk_payload_bytes=...)`) and arrive in order; `offset`
is the index of the chunk's first sample. Between chunks the node sends any waiting
control message and the waiting spectrum frame of every window. Across the chunks of
one capture `capture_id`, `channel_id`, `t_first_ns`, `sample_rate_hz`, `center_hz` and
`n_samples_total` are constant; `overflow` is OR-ed; the clock is the first chunk's
unless a later chunk reports `locked = false`, which then wins. The receiver rejects a
chunk over 256 KiB, `offset + n` beyond `n_samples_total`, `last` set anywhere but on
the final chunk, and an `offset` the earlier chunks could not have reached. Every chunk
holds at least one sample. `n_samples_total` must equal the requested `n_samples`:
another value fails the capture (`bad_data`, with a `capture_cancel`) without counting
as a malformed message.

Node -> `capture_error {type, capture_id, channel_id, code, message}` with codes
`out_of_range` (frequency or rate), `too_late` (interval no longer buffered, or
`deadline_ns` passed on arrival, during the read or during the upload), `overflow`,
`driver` (no open driver for the channel, a driver exception, or unusable samples),
`busy` (more than 8 captures at once), `cancelled`. A `capture_error` after partial
data voids the capture.

Server -> `capture_cancel {type, capture_id}` is advisory, sent on the deadline, when
the caller cancels and when a capture is voided; the node answers with
`capture_error {code: "cancelled"}`. The server gives up on a capture when its own
clock passes `deadline_ns` plus `Settings.capture_grace_s` (1 s by default).

The node reports the true labelled time of its first sample in `t_first_ns` and the
rate and centre actually used; they need not equal the request. The correlator
corrects first-sample and centre differences and rejects rate differences.

Pending captures end with the session: a socket close fails them at once; on
disconnect the node cancels captures in progress and never sends `capture_data` for a
`capture_id` issued in an earlier session; the server discards `capture_data` and
`capture_error` for a `capture_id` that is not pending. Tasking treats every failure as
retryable except `unknown_channel`, `too_large`, `out_of_range` and its own
`invalid_request` (ARCHITECTURE.md section 7).

## Errors and limits

- Unknown `type`: ignored and counted, with a warning logged once per type per socket
  for at most 16 types (so neither a newer peer nor an unauthenticated one can flood
  the log). The node also skips an unknown type while waiting for `welcome`.
- Malformed message: `error {type, code: "bad_message", detail}`; three in a row
  (counting those before `hello`) close the session with 1002. Both ends apply the
  rule; a binary frame from the server is malformed to the node.
- Server enforces per-node limits: at most 8 concurrent captures, 64 MiB per capture.
  The hub does not check `concurrent_reads`, the tuning range or the rate; the node
  answers `busy` or `out_of_range` and tasking reads the capabilities.
- Either side may close with code 1001 for shutdown; nodes reconnect with exponential
  backoff (1 s doubling to 30 s, plus up to 20 % jitter; reset after any session that
  reached `welcome`) and resume from `hello`. After a reject (other than
  `protocol_mismatch`) or a 4001 close the node waits the longest backoff;
  `protocol_mismatch`, or a `welcome` with another protocol, stops it.
- Close codes: 1001 shutdown, 1002 three malformed messages in a row, 1008 rejected or
  site removed, 1011 internal error (and the node's own silence watchdog), 1013 peer not
  reading (the hub abandons a node that has not drained its socket for 3 s), 4001
  replaced, 4002 silent for three heartbeat periods or no `hello` in time.
- WebSocket keepalive pings run every 20 s with a 20 s timeout on both ends.

## Simulation backplane

When the server runs with a scenario, `welcome.simulation.enabled` is true and
`scenario_url` is `/node/v1/sim`, a path on the server origin (the node maps `ws` to
`http` and `wss` to `https`). `GET` with `Authorization: Bearer <node token>` (the
token of any configured site) returns `{version: int, effective_ns: string, config:
SystemConfig, scenario: Scenario}` (a decimal string here because the body is also read
by the console; `sim_update` on the node socket carries the same instant as a JSON
integer). The token is checked first: 401 `{detail}` without a valid node token, then
404 when not simulating. The route is outside the API-token check. A node refuses a
document over 32 MiB, one whose `effective_ns` is not a decimal string of integer ns,
and one whose scenario does not validate against its config.

The server announces every scenario or config change on the node socket with
`sim_update {type, version, effective_ns}`, `effective_ns` = server time + 2 s. The
scenario version doubles as the document version and is also raised by every config
change while simulating, so nodes always refetch. The node fetches immediately and uses
the new world for samples at or after `effective_ns` and the previous world before it;
a simulated node also refetches before sending `assign_ack` (through `on_assign`).
There is no polling. Real drivers never use this channel. How a simulated node switches
worlds is in ARCHITECTURE.md section 6.

# B. Console to server

REST under `/api/v1`, JSON. **Every absolute-time `*_ns` field is a decimal string** on
this interface (JavaScript numbers cannot hold nanosecond timestamps); other times are
ISO 8601 UTC ending in `Z`. The exception is `ClockStatus.est_offset_ns` inside
`NodeStatus`: a signed offset, not an absolute time, sent as a JSON number. Nothing is
ever NaN or infinite; a value that could not be computed is `null`. Unknown fields in
request bodies are errors (422).

When the server has an API token, every `/api/v1` request except `GET /meta` carries
`Authorization: Bearer <token>` (401 `{detail}` with `WWW-Authenticate: Bearer`
otherwise), and the `/ws/ui` handshake carries `?token=` (browsers cannot set headers
on a WebSocket; the server's log filter writes it as `?token=[redacted]`).

## Errors

Every error reply is JSON, never a stack trace:

```
ErrorResponse        {detail}                                every 4xx and 5xx not listed below
ConflictResponse     {detail, current_version}               409 from PUT /config (the ETag header carries the version)
FieldErrorResponse   {detail, errors: [{loc, msg}], warnings: [{loc, msg}]}    every 422
```

`loc` is a dotted path from the document root (`sites.2.channels.0.antenna.diameter_m`)
or the name of the query parameter, without FastAPI's `body` / `query` prefix and
pydantic's union-member names; `(root)` or `""` is the whole document. `detail` repeats
the first error in words. Request validation by FastAPI also answers with
`FieldErrorResponse`. Status codes in use: 400 (unreadable `Content-Length`), 401, 404,
405 (other methods on unknown paths), 409, 413 (body over 4 MiB), 415 (a `Content-Type`
other than JSON or YAML), 422, 428 (`If-Match` missing), 500 (storage failure or an
unexpected error; logged with its traceback), 501 (`core.accuracy` not importable), 503
(server starting, with `Retry-After: 2`; or too many accuracy maps waiting, with
`Retry-After: 10`), 504 (accuracy map over its time limit).

## Routes

```
GET  /meta                      Meta (no token needed)
GET  /config                    ConfigEnvelope, ETag "<version>"; ?format=yaml: the configuration as a YAML download (export_dict, derived monitors left out)
PUT  /config                    replace; body: SystemConfig, a ConfigEnvelope, or YAML; If-Match "<version>" or * required -> ConfigEnvelope, new ETag
                                428 without If-Match; 409 ConflictResponse when stale; 422 FieldErrorResponse (also loc "(scenario)" when the running
                                scenario no longer fits); 413; 415. The body's own version is ignored.
POST /config/validate           ValidateResult {ok, errors, warnings} (always HTTP 200; same bodies as PUT)
GET  /config/schema             JSON Schema (draft 2020-12) of SystemConfig
GET  /status                    Status
GET  /events?state=&limit=      [EventSummary], newest first; state: one of detected, characterizing, locating, located, lost, closed,
                                or active (the first four); absent for all; anything else (also a comma list) 422. limit 1..1000, default 100
GET  /events/{id}               Event (404 for an unknown id, or one dropped from memory: ARCHITECTURE.md section 7)
POST /events/{id}/close         Event (closing a closed event changes nothing)
GET  /events/{id}/lop?step_deg= LopCollection; step_deg 0.05..5, automatic by default (at least 0.25 degree, at most 250 000 cells);
                                422 above 500 000 cells
GET  /geometry/subpoints?t=     [Subpoint]                       (t: ISO 8601 UTC, default now; 422 when not a time)
GET  /geometry/footprints?t=&min_elevation_deg=   [Footprint]    (min_elevation_deg in (-90, 90), default 0)
GET  /geometry/look-angles?t=   [LookAngle]                      (every channel by every satellite)
POST /analysis/accuracy         AccuracyRequest -> AccuracyGrid; 422 above 20 000 cells; 503 when 4 maps already wait; 504 after 120 s
POST /analysis/link-budget      LinkBudgetRequest -> LinkBudgetResponse
GET  /sim/scenario              ScenarioEnvelope                 (404 when not simulating, here and below)
PUT  /sim/scenario              replace; body: Scenario, a ScenarioEnvelope, or YAML -> ScenarioEnvelope; announced to nodes, effective 2 s later
GET  /sim/presets               [Preset] (the *.yaml, *.yml, *.json files of the presets directory, by file stem)
POST /sim/presets/{name}/load   ScenarioEnvelope (announced like PUT)
POST /sim/reset                 ScenarioEnvelope: back to the scenario the server started with
GET  /sim/truth                 SimTruth
```

Outside `/api/v1`: `GET /healthz` (`{status, version, nodes_online}`, no token),
`GET /openapi.json` (the schema; `/docs` loads Swagger UI from a CDN and does not work
offline), the node backplane (section A), and the console build at `/` (paths without a
file extension fall back to `index.html`; paths under `api/`, `ws/` and `node/` never
do).

Bodies of `PUT /config`, `POST /config/validate` and `PUT /sim/scenario` are read by
`Content-Type`: `application/json` (or `+json`), `application/yaml` and its aliases (or
`+yaml`); with none, `text/plain` or `application/octet-stream` a body starting with `{`
is JSON, anything else YAML. A JSON envelope as the server returns it is unwrapped. An
empty body is a 422.

`GET /config` returns the configuration with its derived monitors filled in; the ids of
those channels are listed in `derived_monitors`. Send them back with `monitors: []` to
keep them derived (they then follow transponder edits); a plain GET-then-PUT of the
whole document freezes them as explicit windows.

## Models

```
Meta             {version, marking: {level, line} | null, simulation, server_t_ns, config_name, api_token_required}
ConfigEnvelope   {version, config: SystemConfig, warnings: [{loc, msg}], derived_monitors: [channel_id]}
Status           {server_t_ns, config_version, nodes: [NodeStatus], events_active,
                  queue: {captures_pending, jobs_running, jobs_waiting}}
NodeStatus       {site_id, node_id | null, online, latency_ms | null, clock: ClockStatus | null, driver | null, software | null,
                  channels: [{channel_id, ok, last_frame_age_s | null}], connected_since: ISO | null}
                  # one row per configured site; an offline site keeps its last node_id, clock, driver and software
EventSummary     {id, state, verdict, first_seen_ns, last_seen_ns, uplink_lo_hz | null, uplink_hi_hz | null, satellite_ids,
                  label | null, headline, solution: SolutionSummary | null, simulated}
SolutionSummary  {lat_deg, lon_deg, semi_major_m, semi_minor_m, azimuth_deg, cep50_m, ambiguous,
                  ambiguities: [{lat_deg, lon_deg, semi_major_m, semi_minor_m, azimuth_deg}]}
Event            {id, state, verdict, verdict_reason | null, first_seen_ns, last_seen_ns,
                  uplink_lo_hz | null, uplink_hi_hz | null, downlink_lo_hz | null, downlink_hi_hz | null,
                  satellite_ids, transponders: [{sat_id, transponder_id, downlink_lo_hz, downlink_hi_hz}],
                  classification: {label, confidence, features: {name: number}} | null,
                  advisory: {headline, detail, actions: [text]},
                  evidence: [EvidenceRow], transitions: [{t_ns, state, verdict, reason | null}],
                  solution: SolutionOut | null, solution_t_ns | null,
                  solution_history: [{t_ns, semi_major_m, semi_minor_m, n_measurements}],
                  measurements: [MeasurementRow], caf: CafDisplay | null, truth: EventTruth | null,
                  merged_into | null, simulated, tasking: TaskingStatus | null}
EvidenceRow      {channel_id, site_id, sat_id | null, state: "seen" | "not_seen_capable" | "not_capable" | "stale",
                  snr_db | null, cn0_dbhz | null, predicted_margin_db | null, last_seen_ns | null,
                  monitor_id | null, downlink_lo_hz | null, downlink_hi_hz | null}
MeasurementRow   {t_ns, kind: "tdoa" | "fdoa", pair: {a: {sat_id, site_id, channel_id}, b: {...}}, value | null, sigma | null,
                  residual | null, snr_db | null, calibrated, used, reason | null, correction | null, reference_carrier_id | null}
CafDisplay       {t_ns, pair, surface: [[dB]], delay_axis_s, doppler_axis_hz, delay_cut: {x, db}, doppler_cut: {x, db},
                  peak_tdoa_s | null, peak_fdoa_hz | null, snr_db, detected, reason | null,
                  candidates: [{tdoa_s | null, fdoa_hz, snr_db}]}
TaskingStatus    {state: "waiting" | "capturing" | "processing" | "idle", rounds, last_round_ns | null, next_round_ns | null,
                  channels: [channel_id], message | null, solve_reason | null}
EventTruth       {emitter_id, lat_deg, lon_deg, miss_m | null, inside_ellipse | null}          # simulation only
SolutionOut      core.solver.Solution.as_dict(): {lat_deg, lon_deg, height_m, cov_en_m2, cov_noise_en_m2, cov_consider_en_m2,
                  ellipse: {semi_major_m, semi_minor_m, azimuth_deg, confidence}, cep50_m, chi2, dof, reduced_chi2 | null,
                  residuals, ambiguities: [SolutionOut], ambiguity_resolved_by, converged, dominant_error, n_measurements}
LopCollection    {type: "FeatureCollection", features: [{type: "Feature", geometry, properties: LopProperties}]}
LopProperties    {role: "line" | "band", kind, event_id, seq, tag, a_sat_id, a_site_id, a_channel_id, b_sat_id, b_site_id,
                  b_channel_id, t_ns, value, correction, calibrated, sigma, sigma_total, k_sigma (2), height_m}
Subpoint         {sat_id, lat_deg, lon_deg, alt_m}
Footprint        {sat_id, min_elevation_deg, ring: [[lon, lat], ...]}       # closed; longitudes continuous around the sub-point
LookAngle        {site_id, channel_id, sat_id, az_deg, el_deg, range_km, off_axis_deg, gain_dbi, peak_gain_dbi, freq_hz,
                  visible, watched, electrically_small}
AccuracyRequest  {pairs: [[channel_a, channel_b]], emitter: EmitterModel, f_e_hz, t?: ISO, region?: Region, step_deg (0.25..10, 2),
                  kinds (["tdoa", "fdoa"]), reference_carrier_id?, integration_s?, uncertainty?: UncertaintyConfig}
AccuracyGrid     {t_ns, lat_deg: [..], lon_deg: [..], semi_major_95_m: [[m | null]], cep50_m: [[m | null]], dominant: [[source | null]]}
LinkBudgetRequest  {emitter: EmitterModel, f_e_hz, lat_deg, lon_deg, alt_m (0), t?: ISO}
LinkBudgetResponse {t_ns, rows: [{sat_id, transponder_id, site_id, channel_id, f_down_hz, uplink_elevation_deg,
                  eirp_toward_sat_dbw, pfd_dbw_m2, cn0_up_dbhz, transponder_eirp_dbw, downlink_elevation_deg, off_axis_deg,
                  c_rx_dbw, cn0_total_dbhz, snr_db, detectable, monitor_id | null}]}
ScenarioEnvelope {version, scenario: Scenario}
Preset           {name, description}
SimTruth         {version, emitters: [{id, name, kind, lat_deg, lon_deg, freq_hz, eirp_dbw, bandwidth_hz, waveform, enabled,
                  active}], events: [{event_id, emitter_id | null, miss_m | null, inside_ellipse | null}], simulated: true}
```

Notes on the fields:

- `Event.uplink_lo_hz` / `uplink_hi_hz` are null for an event outside every transponder
  passband (no satellite relays it; it can only be `local` or `undetermined`), and
  `downlink_lo_hz` / `downlink_hi_hz` then say where it was seen. `merged_into` is set
  on an event folded into another (it is then `closed`). `simulated` events have an
  advisory headline starting `Simulation: `.
- `Transition.reason` is plain words: `all detections released`, `detected again`,
  `closed by operator`, `merged into EV-000012`, `split from ...`,
  `server restarted: kept as history`, and the set-aside reasons of ARCHITECTURE.md
  section 7. A document keeps the first and the newest 200 transitions and 500
  solution-history rows, and the newest 1000 measurement rows (the store keeps all).
- `EvidenceRow.sat_id` is null for an event outside every passband.
  `predicted_margin_db` is how far above the channel's threshold the signal should be
  given the strongest sighting (null when it cannot be predicted). `last_seen_ns` is
  the newest frame in which the channel's detection of the event was above threshold,
  also after the detection ended (so `stale` and `not_seen_capable` rows say how old
  the sighting is); null when the channel never saw it.
- `MeasurementRow.value` is A minus B in s or Hz, after calibration when `calibrated`;
  `correction` is then the reference-carrier correction already subtracted (the raw
  measurement is `value + correction`) and `reference_carrier_id` the carrier it was
  measured on; both null otherwise. `residual` is whitened against the current
  solution, null when the row is not in it. `reason` (when `used` is false):
  `not_detected`, `ambiguous`, `delay_doppler_coupled`, `narrowband` (a TDOA row of a
  signal too narrow for a time difference; the FDOA row is used), `outside_window`,
  `mirror_ambiguous`, `clock_unlocked`, `rate_mismatch`, `overflow`,
  `unconfirmed: awaiting a repeat detection at a consistent TDOA/FDOA`,
  `inconsistent with this pair's earlier detections`,
  `window_exceeds_capture: <detail>`, `capture_failed: <code>: <message>`,
  `correlation error: <detail>`, and `set aside: <why>` for rows withdrawn when the
  event's earlier position was set aside (value kept, residual null). A pair whose two
  kinds failed differently reads `tdoa: <reason>; fdoa: <reason>`.
- `CafDisplay` is the strongest detected pair of the latest round (the latest pair
  when none was detected): a patch of at most 65 x 65 cells in dB over the noise floor,
  cuts through the peak, and up to 8 distinct peaks strongest first (`tdoa_s` null on
  the Doppler ridge of a CW carrier; peaks may lie outside the patch).
- `TaskingStatus` is null until tasking has looked at an event. `idle` means nothing to
  capture (no usable channel, no second relaying satellite, or the event was judged
  `local`; `message` says which). `message` describes the last completed round and stays
  while the next one runs; `solve_reason` says why the last solve gave no position
  (`unobservable`, `fewer than two accepted measurements`, ...).
- `AccuracyRequest.t` defaults to the start of the current minute (so a repeated
  request is answered from the cache of 8 maps). `uncertainty` replaces
  `processing.uncertainty` for that map only; satellites with their own
  `ephemeris_sigma` keep it and site clocks keep `Site.clock.sigma_ns`. One map is
  computed at a time, up to 4 wait; the grid is computed in latitude bands of about 256
  cells and the 120 s limit is checked between bands.
- `LinkBudgetRow` exists only for paths that exist (satellite above both horizons,
  frequency inside the transponder). `snr_db` is in the emitter's bandwidth (1 Hz for
  zero width); `detectable` is `10 log10(1 + C / (N0 max(bandwidth, bin))) >=
  processing.detection.threshold_db` in the bins of the monitor covering `f_down_hz`
  (`monitor_id`; null and not detectable when no window covers it).
- `SimTruth.events` covers the active events. Truth is fictional and labelled
  simulated wherever it is shown.

## WebSocket `/ws/ui`

Server to browser, text frames:

- `status {type, server_t_ns, config_version, marking, simulation, nodes: [NodeStatus], events_active, queue}` about once a second (a new client gets the latest one at once)
- `event {type, event: Event}` (the whole object on every published change)
- `config {type, config_version}` when configuration is replaced
- `sim {type, version}` when the scenario changes

An event is published at once on a material change (state, verdict, evidence states,
satellites, classification, a measurement, a solution, a correlation surface) and at
most every 5 s for numeric drift (SNR, C/N0, margins, edges).

Binary frames reuse the `PLB1` framing with header
`{type: "psd", channel_id, monitor_id, t_ns: "<string>", center_hz, span_hz, nfft, seq,
has_baseline, detections: [{track_id, f_lo_hz, f_hi_hz, authorized_carrier_id, anomaly}]}`
(`authorized_carrier_id` is written as null, not omitted) and payload `float32[nfft]`
PSD followed by `float32[nfft]` baseline when `has_baseline` (false until the tracker
has learned a baseline, since a NaN payload is never sent).

Browser to server: `subscribe {type, psd: [[channel_id, monitor_id], ...]}` (at most
256 windows) replaces the streamed selection. Other messages are ignored (the first
three logged). Each client has a mailbox in which a newer message of the same kind
(status, config, sim, one event id, one spectrum window) replaces an undelivered older
one, so a slow browser only loses its own intermediate frames and never blocks the
pipeline or other clients.

Close codes: 1008 for a missing or wrong `?token=` (the handshake is accepted and closed
at once so the browser learns why), 1013 while the server is starting or when a frame
has taken longer than 30 s to leave (`ws.SEND_TIMEOUT_S`; a very slow link receiving a
large event may need more), 1001 at shutdown and for handshakes during shutdown, 1011
for an internal error, 1000 when the browser leaves.
