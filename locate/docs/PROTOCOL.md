# PETAL Locate — wire protocols (version 1)

Two interfaces: nodes to server (section A) and operator console to server
(section B).

# A. Node to server

Transport: one WebSocket per node, `ws(s)://server/ws/node`. Control messages are text
frames holding one JSON object with a `type` field. Sample and spectrum data are binary
frames. The node always dials out, so sites behind NAT need no inbound ports.
Production deployments terminate TLS in front of the server (`wss`); the token below
is a bearer secret and must not travel in clear text outside a lab.

All absolute times are integer UTC nanoseconds (`*_ns`), sent as JSON integers on this
interface (both ends are Python). `t_first_ns` and every `t_ns` are **labelled** times
(the node's clock).

permessage-deflate is disabled on both ends (`websockets.connect(compression=None)`;
uvicorn `ws_per_message_deflate=False`, `ws_max_size >= 1 MiB`): IQ does not compress
and deflate costs 139 ms of event loop per 4 MiB.

## Binary frame

```
bytes 0..3    magic  b"PLB1"
bytes 4..7    header length H, uint32 little-endian
bytes 8..8+H  header, UTF-8 JSON object with a "type" field
remainder     payload, little-endian
```

Receivers reject frames with a wrong magic, an oversized header (> 64 KiB), or a
payload whose length does not match the header.

## Session

1. Node -> `hello`
   `{type, protocol: 1, node_id, site_id, token, software, driver, clock: ClockStatus, t_ns}`
   `node_id` is a UUID the node generates once and persists (or `--node-id`).
2. Server -> `welcome`
   `{type, protocol: 1, server_t_ns, heartbeat_s (default 2), config_version,
   channels: [ChannelAssignment], simulation: {enabled, scenario_url?, version?}}`
   or `reject {type, reason}` followed by close. Reasons: `bad_token` (wrong token, or
   the server holds no token for the site), `unknown_site`, `protocol_mismatch`,
   `duplicate_node` (a *different* node_id holds the site).
   A hello whose node_id equals that of the live session for the site **replaces** it:
   the server closes the old socket with code 4001, fails its pending captures and
   continues with `welcome`.
3. After `welcome` and after every `assign` the node opens one driver per channel and
   sends `assign_ack {type, config_version, ok, channels: [{channel_id, ok, error?,
   capabilities: {min_hz, max_hz, max_sample_rate_hz, lookback_s, concurrent_reads}}]}`.
   The server does not task a channel before its ack; tasking uses
   `min(config max_sample_rate_hz, capabilities.max_sample_rate_hz)`.
4. Node -> `heartbeat {type, seq, t_ns, rtt_ms, clock: ClockStatus, load: {cpu, queue}}`
   every `heartbeat_s` (`rtt_ms` = node-measured round trip of the previous
   heartbeat); server -> `heartbeat_ack {type, seq, server_t_ns}`. Any frame received
   from a node counts as liveness. Three heartbeat periods of silence mark the node
   offline and fail its pending captures.
5. Server -> `assign {type, config_version, channels: [ChannelAssignment]}` whenever
   configuration changes. An assign fails pending captures on removed or changed
   channels.

```
ChannelAssignment  {channel_id, antenna_summary, monitors: [{monitor_id, center_hz, span_hz, nfft, navg, frame_rate_hz}]}
ClockStatus        {source: "gpsdo" | "ntp" | "sim" | "free", locked: bool, est_offset_ns: number, holdover_s: number}
```

`est_offset_ns` is the node's estimate of `delta_g` of MATH.md section 4 (true time
minus labelled time). It is displayed and never applied to measurements. A capture
whose ClockStatus has `locked = false` yields no TDOA unless both channels of the pair
are at the same site.

## Spectrum frames (node -> server, binary)

Header `{type: "psd", channel_id, monitor_id, t_ns, center_hz, span_hz, nfft, navg,
seq, units: "dB"}`; payload `float32[nfft]`, fft-shifted so bin `k` is at
`center_hz + (k - nfft/2) * span_hz / nfft`; `t_ns` is the first-sample time.

PSD frames are droppable: the node keeps at most one unsent frame per monitor (newest
wins), so gaps in `seq` are normal.

## Captures

Server -> `capture_request`
`{type, capture_id, channel_id, t_start_ns, n_samples, sample_rate_hz, center_hz,
deadline_ns}`

`t_start_ns` is normally a little in the future. It may be in the past when the driver
advertises `lookback_s` and still holds that interval. `deadline_ns` is the node-clock
time by which the last chunk must be sent.

Node -> one or more binary frames, header
`{type: "capture_data", capture_id, channel_id, seq, last: bool, t_first_ns,
sample_rate_hz, center_hz, n_samples_total, offset, n, overflow: bool, clock:
ClockStatus}`; payload `complex64[n]` (interleaved float32 I, Q). Chunks are at most
256 KiB of payload and arrive in order; `offset` is the index of the chunk's first
sample. The node yields between chunks so heartbeats and PSD frames interleave.

Node -> `capture_error {type, capture_id, channel_id, code, message}` with codes
`out_of_range` (frequency or rate), `too_late` (interval no longer buffered),
`overflow`, `driver`, `busy`, `cancelled`. A `capture_error` after partial data voids
the capture.

Server -> `capture_cancel {type, capture_id}` is advisory.

The node reports the true labelled time of its first sample in `t_first_ns` and the
rate and centre actually used; they need not equal the request. The correlator
corrects first-sample and centre differences and rejects rate differences.

Pending captures end with the session: a socket close fails them at once; on
disconnect the node cancels captures in progress and never sends `capture_data` for a
`capture_id` issued in an earlier session; the server discards `capture_data` and
`capture_error` for a `capture_id` that is not pending. Tasking treats all of these as
retryable.

## Errors and limits

- Unknown `type`: ignored with a logged warning (forward compatibility).
- Malformed message: `error {type, code: "bad_message", detail}`; three in a row
  close the session.
- Server enforces per-node limits: at most 8 concurrent captures, 64 MiB per capture.
- Either side may close with code 1001 for shutdown; nodes reconnect with exponential
  backoff (1 s doubling to 30 s, plus up to 20 % jitter) and resume from `hello`.

## Simulation backplane

When the server runs with a scenario, `welcome.simulation.enabled` is true.
`scenario_url` is a path on the server origin (the node maps `ws` to `http` and `wss`
to `https`). `GET` with `Authorization: Bearer <node token>` returns
`{version: int, effective_ns: string, config: SystemConfig, scenario: Scenario}`.
The server announces every scenario or config change on the node socket with
`sim_update {type, version, effective_ns}`, `effective_ns` = server time + 2 s. The
node fetches immediately and uses the new world for samples at or after
`effective_ns` and the previous world before it; a simulated node also refetches
before sending `assign_ack`. There is no polling. Real drivers never use this channel.

# B. Console to server

REST under `/api/v1`, JSON. **Every `*_ns` field is a decimal string** on this
interface (JavaScript numbers cannot hold nanosecond timestamps); other times are ISO
8601 UTC. When the server has an API token, every request carries
`Authorization: Bearer <token>` and the WebSocket handshake carries `?token=`.
FastAPI's `/openapi.json` is the authoritative schema; this section fixes the shape.

```
GET  /meta                      {version, marking: {level, line} | null, simulation: bool, server_t_ns}
GET  /config                    SystemConfig, with ETag = version
PUT  /config                    If-Match required; 409 on version conflict; 422 with field errors
POST /config/validate           {ok, errors: [{loc, msg}], warnings: [{loc, msg}]}
GET  /config/schema             JSON schema of SystemConfig
GET  /status                    {server_t_ns, config_version, nodes: [NodeStatus], events_active, queue}
GET  /events?state=&limit=      [EventSummary]
GET  /events/{id}               Event (with measurements, solution history, latest CAF display data)
POST /events/{id}/close
GET  /events/{id}/lop           GeoJSON FeatureCollection of the lines of position of the latest measurements
GET  /geometry/subpoints?t=     [{sat_id, lat_deg, lon_deg, alt_m}]
GET  /geometry/footprints?t=&min_elevation_deg=   [{sat_id, ring: [[lon, lat], ...]}]
GET  /geometry/look-angles?t=   [{site_id, channel_id, sat_id, az_deg, el_deg, range_km, off_axis_deg, gain_dbi, electrically_small}]
POST /analysis/accuracy         accuracy_map request -> grid (ARCHITECTURE.md section 5)
POST /analysis/link-budget      {emitter, f_e_hz, lat_deg, lon_deg, ...} -> per (satellite, channel) PathBudget
GET  /sim/scenario              Scenario with version        (404 when not simulating)
PUT  /sim/scenario              replace; announced to nodes with sim_update
GET  /sim/presets               [{name, description}]
POST /sim/presets/{name}/load
POST /sim/reset                 back to the scenario the server started with
GET  /sim/truth                 emitters with truth positions and, per active event, miss distance
```

```
NodeStatus   {site_id, node_id, online, latency_ms, clock: ClockStatus, driver, software,
              channels: [{channel_id, ok, last_frame_age_s}], connected_since}
Event        {id, state, verdict, first_seen_ns, last_seen_ns, uplink_lo_hz, uplink_hi_hz,
              satellite_ids, classification: {label, confidence, features} | null, advisory: {headline, detail, actions: [..]},
              evidence: [{channel_id, site_id, sat_id, state: "seen"|"not_seen_capable"|"not_capable"|"stale", snr_db, cn0_dbhz, predicted_margin_db}],
              transitions: [{t_ns, state, verdict}],
              solution: Solution | null, solution_history: [{t_ns, semi_major_m, semi_minor_m, n_measurements}],
              measurements: [{t_ns, kind, pair: {a: {sat_id, site_id, channel_id}, b: {...}}, value, sigma, residual, snr_db, calibrated, reason}],
              caf: {pair, surface, delay_axis_s, doppler_axis_hz, delay_cut, doppler_cut} | null,
              truth: {lat_deg, lon_deg, miss_m, inside_ellipse} | null}       # simulation only
Solution     ARCHITECTURE.md section 5 Solution as JSON (matrices as nested lists)
```

WebSocket `/ws/ui`, server to browser, text frames:

- `status {type, server_t_ns, config_version, marking, simulation, nodes: [NodeStatus]}` about once a second
- `event {type, event: Event}` (the whole object on every change)
- `config {type, config_version}` when configuration is replaced
- `sim {type, version}` when the scenario changes

Binary frames reuse the `PLB1` framing with header
`{type: "psd", channel_id, monitor_id, t_ns: "<string>", center_hz, span_hz, nfft, seq,
has_baseline, detections: [{track_id, f_lo_hz, f_hi_hz, authorized_carrier_id, anomaly}]}`
and payload `float32[nfft]` PSD followed by `float32[nfft]` baseline when
`has_baseline`.

Browser to server: `subscribe {type, psd: [[channel_id, monitor_id], ...]}` selects
which windows are streamed. The server keeps at most one unsent PSD frame per window
per client (newest wins) and never blocks other clients on a slow one.
