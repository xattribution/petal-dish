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
**labeled** times (the node's clock). The one `*_ns` field that is not an absolute
time, `ClockStatus.est_offset_ns`, is a JSON number. Under `petal-locate demo --start`
the server's clock and every simulated node's clock are the host clock plus one fixed
offset (simulation only), so every time on both interfaces is simulated time.

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
- Text must be valid Unicode: a JSON string or key holding an unpaired UTF-16 surrogate
  (the escape `\ud800` on its own) makes a control message or binary header malformed
  (`<control message|binary frame header>: <field.path>: text that is not valid Unicode
  (an unpaired surrogate)`). The node escapes such characters in text it passes on from
  its driver or peer (`hello.software` and `driver`, `capture_error.message`, a channel
  error in `assign_ack`) as `\udce9`; a `capture_error` whose message cannot be built is
  sent with `message: ""`.
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
minus labeled time). It is displayed and never applied to measurements. A capture
whose ClockStatus has `locked = false` yields no TDOA unless both channels of the pair
are at the same site. The shipped `SystemClock` reports `ntp`, locked, without asking
the operating system whether NTP is synchronised; a node run with `--driver sim`
reports `sim`, locked; `--clock` and `--clock-unlocked` override what is reported
(OPERATIONS.md).

## Spectrum frames (node -> server, binary)

Header `{type: "psd", channel_id, monitor_id, t_ns, center_hz, span_hz, nfft, navg,
seq, units: "dB"}`; payload `float32[nfft]`, fft-shifted so bin `k` is at
`center_hz + (k - nfft/2) * span_hz / nfft`; `t_ns` is the first-sample time.

PSD frames are droppable: the node keeps at most one unsent frame per monitor (newest
wins), so gaps in `seq` are normal. `center_hz`, `span_hz` and `t_ns` are what the
driver actually measured. `seq` restarts at 0 when a channel is reopened or a session
starts. The server drops (and counts, without treating them as malformed) frames for
channels not acknowledged under the current assignment, windows that are not assigned,
or a different `nfft`, and frames stamped more than `PSD_FUTURE_TOLERANCE_S` (60 s)
ahead of the server clock (counter `psd_dropped_future`, one warning per session; such a
frame does not count as the window's newest). Its detection pipeline also drops frames
whose center moved by more than half a bin, that hold a non-finite bin, or whose `t_ns`
repeats or goes back by up to three frame periods. A frame more than three frame
periods older than the newest accepted is taken as a step of the node's clock: it is
accepted as the new time base (the learned baseline is kept) and warned about once per
window. A new session of a site resets its windows' frame times.

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
44 kbit/s uplink; `petal-locate node --chunk-payload-bytes`,
`NodeAgent(chunk_payload_bytes=...)`) and arrive in order; `offset`
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
`capture_error {code: "cancelled"}`. The node logs a capture the server withdrew (a
`capture_cancel`, or an `assign` that removed or changed its channel) at INFO as
`capture withdrawn by the server: cancelled: ...`; real failures stay WARNING
`capture failed: ...`. The server gives up on a capture when its own
clock passes `deadline_ns` plus `Settings.capture_grace_s` (1 s by default).

The node reports the true labeled time of its first sample in `t_first_ns` and the
rate and center actually used; they need not equal the request. The correlator
corrects first-sample and center differences and rejects rate differences.

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
  A capture over the 8 is not sent: it waits (nothing on the wire) until one of the
  node's slots frees, the session changes or its deadline passes, and only then fails
  with the server's own `limit` code ("... had 8 captures pending until the deadline";
  counters `capture_queued`, `capture_refused_limit`). The slots are shared by every
  caller, so one event's round never makes another's fail at once. The hub does not
  check `concurrent_reads`, the tuning range or the rate; the node answers `busy` or
  `out_of_range` and tasking reads the capabilities.
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
404 when not simulating. The route is outside the API-token and `Host` checks of
section B, as are `/ws/node` and `/healthz`. A node refuses a
document over 32 MiB, one whose `effective_ns` is not a decimal string of integer ns,
and one whose scenario does not validate against its config.

The server announces every scenario or config change on the node socket with
`sim_update {type, version, effective_ns}`, `effective_ns` = server time + 2 s. The
scenario version doubles as the document version and is also raised by every config
change while simulating, so nodes always refetch. The node fetches immediately and uses
the new world for samples at or after `effective_ns` and the previous world before it;
a simulated node also refetches before sending `assign_ack` (through `on_assign`).
The node hands a `sim_update` to every driver already open and remembers the newest
one of the session, which it hands to each driver that opens later (one that arrived
while its channels were opening is not lost). For nodes older than that, the server
also sends the newest `sim_update` again right after an accepted `assign_ack` whenever
one was sent while that session's assignment was unacknowledged (between `welcome` or
`assign` and the ack; counter `sim_update_resent`). A node may thus receive the same
`sim_update` twice; the sim driver ignores a version it holds. There is no polling. Real drivers never use
this channel. How a simulated node switches worlds is in ARCHITECTURE.md section 6.

# B. Console to server

REST under `/api/v1`, JSON. **Every absolute-time `*_ns` field is a decimal string** on
this interface (JavaScript numbers cannot hold nanosecond timestamps); other times are
ISO 8601 UTC ending in `Z`. The exception is `ClockStatus.est_offset_ns` inside
`NodeStatus`: a signed offset, not an absolute time, sent as a JSON number (fields such
as `SiteClockEstimate.offset_us` are offsets in microseconds, plain numbers). Nothing is
ever NaN or infinite; a value that could not be computed is `null`. Unknown fields in
request bodies are errors (422), and so is text holding an unpaired UTF-16 surrogate
(`<path>: text that is not valid Unicode (an unpaired surrogate)`).

When the server has an API token, every `/api/v1` request except `GET /meta` carries
`Authorization: Bearer <token>` (401 `{detail}` with `WWW-Authenticate: Bearer`
otherwise), and the `/ws/ui` handshake carries `?token=` (browsers cannot set headers
on a WebSocket; the server's log filter writes it as `?token=[redacted]`). `GET /` (or
a console route) with `?token=<the API token>` returns the console page with a script
that stores the token in `sessionStorage` (`petal.apiToken`) and removes it from the
address bar (headers `Cache-Control: no-store`, `Referrer-Policy: no-referrer`); a
wrong or absent token serves the page unchanged. `petal-locate demo` prints such an
address with a random per-run token.

Checks before any route, whether or not there is a token:

- `Host`: every path except `/ws/node`, `/healthz` and the node backplane must carry a
  `Host` whose host part is the bound address, `localhost`, `127.0.0.1`, `::1`, a name
  given with `--allowed-host` (`*` for any) or, when bound to `0.0.0.0` or `::`, this
  machine's host name, fully qualified name and addresses. Otherwise HTTP 400 `{detail:
  "the host name '<host>' is not one this server answers to; start it with
  --allowed-host NAME to allow it"}`, or a refused WebSocket handshake (HTTP 403). A
  request without `Host` passes.
- Request kind: a POST, PUT, PATCH or DELETE under `/api` whose `Content-Type` is empty,
  `text/plain`, `application/x-www-form-urlencoded` or `multipart/form-data` (what a
  web page can send cross-site unasked) and that carries neither `Authorization` nor
  `X-Petal-Client` is refused 403 `{detail: "a state-changing request must carry
  Content-Type application/json or application/yaml, an Authorization header, or the
  header X-Petal-Client (cross-site request defense)"}`. The console sends
  `X-Petal-Client: console` on every such request.
- Size: the body of every such request is limited to 4 MiB (`rest.MAX_BODY_BYTES`,
  `Content-Length` or bytes streamed): 413 `{detail: "the request body is larger than
  4194304 bytes"}`.
- `/ws/ui` `Origin`: a handshake whose `Origin` is neither the server's own
  (`http(s)://<Host>`, the scheme following `X-Forwarded-Proto` from a trusted proxy)
  nor given with `--allowed-origin` (`*` for any) is accepted and closed with 1008
  `this origin may not open the console socket`. A handshake without `Origin` passes.

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
`FieldErrorResponse`. Status codes in use: 400 (unreadable `Content-Length`, or a `Host`
the server does not answer to), 401, 403 (a state-changing request of a kind a foreign
web page could send), 404 (also a static path holding a NUL or one the file system
cannot look up), 405 (other methods on unknown paths), 409, 413 (body over 4 MiB, on
every state-changing `/api` route), 415 (a `Content-Type` other than JSON or YAML), 422
(also a time outside 1970-01-01 to 2262-04-11 UTC, loc `t`: `outside the times the
server represents (1970-01-01 to 2262-04-11 UTC)`), 428 (`If-Match` missing), 500
(storage failure or an unexpected error; logged with its traceback), 501
(`core.accuracy` not importable), 503 (server starting, with `Retry-After: 2`; or too
many accuracy maps waiting, with `Retry-After: 10`), 504 (accuracy map over its time
limit). Every error reply escapes text that is not valid Unicode, so it can always be
written.

## Routes

```
GET  /meta                      Meta (no token needed)
GET  /config                    ConfigEnvelope, ETag "<version>"; ?format=yaml: the configuration as a YAML download (export_dict, derived monitors left out)
PUT  /config                    replace; body: SystemConfig, a ConfigEnvelope, or YAML; If-Match "<version>" or * required -> ConfigEnvelope, new ETag
                                428 without If-Match; 409 ConflictResponse when stale; 422 FieldErrorResponse (also loc "(scenario)" when the running
                                scenario no longer fits); 413; 415. The body's own version is ignored.
POST /config/validate           ValidateResult {ok, errors, warnings} (always HTTP 200; same bodies as PUT; a document nested too deeply
                                gives ok false with {loc "(root)", msg "the document is nested too deeply"}, which PUT answers 422)
GET  /config/schema             JSON Schema (draft 2020-12) of SystemConfig
GET  /status                    Status
GET  /events?state=&limit=      [EventSummary], newest first; state: one of detected, characterizing, locating, located, lost, closed,
                                or active (the first four); absent for all; anything else (also a comma list) 422. limit 1..1000, default 100
GET  /events/{id}               Event (404 for an unknown id, or one dropped from memory: ARCHITECTURE.md section 7)
POST /events/{id}/close         Event (closing a closed event changes nothing)
GET  /events/{id}/lop?step_deg= LopCollection; step_deg 0.05..5, automatic by default (at least 0.25 degree, at most 250 000 cells);
                                422 above 500 000 cells
GET  /geometry/subpoints?t=     [Subpoint]                       (t: ISO 8601 UTC, default now; 422 when not a time or outside 1970..2262)
GET  /geometry/footprints?t=&min_elevation_deg=   [Footprint]    (min_elevation_deg in (-90, 90), default 0)
GET  /geometry/look-angles?t=   [LookAngle]                      (every channel by every satellite)
POST /analysis/accuracy         AccuracyRequest -> AccuracyGrid; 422 above 20 000 cells, above 128 pairs, for a pair listed twice
                                ([A, B] and [B, A] count as one), or when two grid rows would need more than 256 MiB;
                                503 when 4 maps already wait; 504 after 120 s
POST /analysis/link-budget      LinkBudgetRequest -> LinkBudgetResponse (satellites whose orbit cannot be evaluated at t are left out)
GET  /sim/scenario              ScenarioEnvelope                 (404 when not simulating, here and below)
PUT  /sim/scenario              replace; body: Scenario, a ScenarioEnvelope, or YAML -> ScenarioEnvelope; announced to nodes, effective 2 s later
                                (YAML aliases expanding beyond 1 000 000 nodes, duplicate keys and too-deep nesting are 422, loc "(root)")
GET  /sim/presets               [Preset] (the *.yaml, *.yml, *.json files of the presets directory, by file stem)
POST /sim/presets/{name}/load   ScenarioEnvelope (announced like PUT)
POST /sim/reset                 ScenarioEnvelope: back to the scenario the server started with
GET  /sim/truth                 SimTruth
```

Outside `/api/v1`: `GET /healthz` (`{status, version, nodes_online}`, no token),
`GET /openapi.json` (the schema; `/docs` loads Swagger UI from a CDN and does not work
offline), the node backplane (section A), and the console build at `/` (paths without a
file extension fall back to `index.html`; paths under `api/`, `ws/` and `node/` never
do; a query string on an asset, such as the console's `?retry=N` after a failed chunk
load, is ignored).

Bodies of `PUT /config`, `POST /config/validate` and `PUT /sim/scenario` are read by
`Content-Type`: `application/json` (or `+json`), `application/yaml` and its aliases (or
`+yaml`); with `application/octet-stream` (or, from a client that passes the
request-kind check with `Authorization` or `X-Petal-Client`, none or `text/plain`) a
body starting with `{` is JSON, anything else YAML. A JSON envelope as the server
returns it is unwrapped. An empty body is a 422. YAML is read with unique keys, aliases
expanding to at most 1 000 000 nodes and bounded nesting.

`GET /config`, the reply of `PUT /config` and `POST /config/validate` (when `ok`) add a
warning per site whose node has no token on the server: `{loc: "sites.<index>", msg:
"site '<id>' has no node token on this server (none in the node tokens and no
PETAL_NODE_TOKEN_<SITE> in the server's environment), so its node cannot connect;
<remedy>"}`, the remedy naming the node-tokens file (re-read when it changes) or a
restart with `--node-tokens`.

`GET /config` returns the configuration with its derived monitors filled in; the ids of
those channels are listed in `derived_monitors`. Send them back with `monitors: []` to
keep them derived (they then follow transponder edits); a plain GET-then-PUT of the
whole document freezes them as explicit windows.

## Models

```
Meta             {version, marking: {level, line} | null, simulation, server_t_ns, config_name, api_token_required}
ConfigEnvelope   {version, config: SystemConfig, warnings: [{loc, msg}], derived_monitors: [channel_id]}
Status           {server_t_ns, config_version, nodes: [NodeStatus], events_active,
                  queue: {captures_pending, jobs_running, jobs_waiting},
                  satellites: [SatelliteStatus], site_clocks: [SiteClockEstimate]}
NodeStatus       {site_id, node_id | null, online, latency_ms | null, clock: ClockStatus | null, driver | null, software | null,
                  channels: [ChannelStatus], connected_since: ISO | null}
                  # one row per configured site; an offline site keeps its last node_id, clock, driver and software
ChannelStatus    {channel_id, ok, last_frame_age_s | null, windows: [WindowStatus], detection_problem | null}
WindowStatus     {monitor_id, last_frame_age_s | null, detection_frame_age_s | null, blind, blind_reason | null,
                  learning | null, floor_raised_db | null, note | null}
SatelliteStatus  {sat_id, ephemeris_ok, unavailable_since_ns | null, message | null}
SiteClockEstimate {site_id, offset_us, sigma_us | null, n_calibrations, compared_with: [site_id], caution, message | null,
                   suspect_satellite_ids: [sat_id]}
EventSummary     {id, state, verdict, first_seen_ns, last_seen_ns, uplink_lo_hz | null, uplink_hi_hz | null, satellite_ids,
                  label | null, headline, solution: SolutionSummary | null, simulated,
                  onset_ns | null, received_lo_hz | null, received_hi_hz | null, seen_site_ids, width_lower_bound}
SolutionSummary  {lat_deg, lon_deg, semi_major_m, semi_minor_m, azimuth_deg, cep50_m, ambiguous,
                  ambiguities: [{lat_deg, lon_deg, semi_major_m, semi_minor_m, azimuth_deg}], fit_consistent, is_fix}
Event            {id, state, verdict, verdict_reason | null, first_seen_ns, last_seen_ns,
                  uplink_lo_hz | null, uplink_hi_hz | null, downlink_lo_hz | null, downlink_hi_hz | null,
                  satellite_ids, transponders: [{sat_id, transponder_id, downlink_lo_hz, downlink_hi_hz}],
                  not_visible_satellite_ids,
                  classification: {label, confidence, features: {name: number}} | null,
                  advisory: {headline, detail, actions: [text]},
                  evidence: [EvidenceRow], transitions: [{t_ns, state, verdict, reason | null}],
                  solution: SolutionOut | null, solution_t_ns | null,
                  solution_history: [{t_ns, semi_major_m, semi_minor_m, n_measurements, fit_consistent, is_fix, set_aside}],
                  measurements: [MeasurementRow], caf: CafDisplay | null, truth: EventTruth | null,
                  merged_into | null, simulated, tasking: TaskingStatus | null,
                  onset_ns | null, resumed_ns | null, break_s | null, recheck | null,
                  received_lo_hz | null, received_hi_hz | null, seen_site_ids, width_lower_bound,
                  doubted_satellite_ids: [sat_id]}
EvidenceRow      {channel_id, site_id, sat_id | null, state: "seen" | "not_seen_capable" | "not_capable" | "stale",
                  snr_db | null, cn0_dbhz | null, predicted_margin_db | null, last_seen_ns | null,
                  monitor_id | null, downlink_lo_hz | null, downlink_hi_hz | null}
MeasurementRow   {t_ns, kind: "tdoa" | "fdoa", pair: {a: {sat_id, site_id, channel_id}, b: {...}}, value | null, sigma | null,
                  residual | null, snr_db | null, calibrated, used, reason | null, correction | null, reference_carrier_id | null}
CafDisplay       {t_ns, pair, surface: [[dB]], delay_axis_s, doppler_axis_hz, delay_cut: {x, db}, doppler_cut: {x, db},
                  peak_tdoa_s | null, peak_fdoa_hz | null, snr_db, detected, reason | null,
                  candidates: [{tdoa_s | null, fdoa_hz, snr_db}]}
TaskingStatus    {state: "waiting" | "capturing" | "processing" | "idle", rounds, last_round_ns | null, next_round_ns | null,
                  channels: [channel_id], message | null, solve_reason | null, unlocatable_reason | null}
EventTruth       {emitter_id, lat_deg, lon_deg, miss_m | null, inside_ellipse | null}          # simulation only
SolutionOut      core.solver.Solution.as_dict(): {lat_deg, lon_deg, height_m, cov_en_m2, cov_noise_en_m2, cov_consider_en_m2,
                  ellipse: {semi_major_m, semi_minor_m, azimuth_deg, confidence}, cep50_m, chi2, dof, reduced_chi2 | null,
                  residuals, ambiguities: [SolutionOut], ambiguity_resolved_by, converged, dominant_error, n_measurements}
                  plus fit_consistent (true), fit_p_value | null, ellipse_scale (1.0), is_fix (true), not_fix_reason | null,
                  ephemeris_allowance: [EphemerisAllowanceOut] ([]), ellipse_widening | null
EphemerisAllowanceOut {sat_id, scale, sigma_ric_m: [radial, in_track, cross_track], capped (false)}
LopCollection    {type: "FeatureCollection", features: [{type: "Feature", geometry, properties: LopProperties}]}
LopProperties    {role: "line" | "band", kind, event_id, seq, tag, a_sat_id, a_site_id, a_channel_id, b_sat_id, b_site_id,
                  b_channel_id, t_ns, value, correction, calibrated, sigma, sigma_total, k_sigma (2), height_m}
Subpoint         {sat_id, lat_deg, lon_deg, alt_m}
Footprint        {sat_id, min_elevation_deg, ring: [[lon, lat], ...]}       # closed; longitudes continuous around the sub-point
LookAngle        {site_id, channel_id, sat_id, az_deg, el_deg, range_km, off_axis_deg, gain_dbi, peak_gain_dbi, freq_hz,
                  visible, watched, electrically_small}
AccuracyRequest  {pairs: [[channel_a, channel_b]] (at most 128), emitter: EmitterModel, f_e_hz, t?: ISO, region?: Region,
                  step_deg (0.25..10, 2), kinds (["tdoa", "fdoa"]), reference_carrier_id? (a carrier id or "nearest"), integration_s?,
                  uncertainty?: UncertaintyConfig}
AccuracyGrid     {t_ns, lat_deg: [..], lon_deg: [..], semi_major_95_m: [[m | null]], cep50_m: [[m | null]], dominant: [[source | null]],
                  pairs: [AccuracyPairSummary]}
AccuracyPairSummary {a_channel, b_channel, a_sat_id | null, b_sat_id | null, calibrated: ["tdoa" | "fdoa"],
                  reference_carrier_id | null, reference_cells: {carrier_id | "none": cells}, uncalibrated_cells,
                  reference_reason | null, reason | null, notes: [..]}
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
- `Event.satellite_ids` and `transponders` (also in `EventSummary`) are the frequency
  plan until a consistent fix exists; after that only the satellites above
  `max(solver.min_elevation_deg, 0)` at the fix, at an end of either 95 % ellipse axis
  or at any ambiguity (never none; a satellite whose orbit cannot be evaluated is
  kept). The others are listed in `not_visible_satellite_ids`: they relay the frequency
  but cannot see the transmitter, and tasking does not pair them. All three are empty
  while the verdict is `local` (no satellite relays a local signal; `uplink_lo_hz` and
  `uplink_hi_hz` still give the uplink frequencies its received range maps to), and
  such an event reads `detected` (transition `judged local: nothing to characterize or
  locate by satellite`).
- `onset_ns` is the start of the current transmission (labeled time): `first_seen_ns`
  unless the event broke off or another transmitter took over (then the first
  detection after the break, or the first that showed the other transmitter); when a
  local signal takes over an uplink event's frequency (verdict uplink to local) it is
  the last sighting of the uplink signal at the sites that no longer see anything; when
  an uplink signal takes over a local event's frequency (verdict local to uplink) it is
  its first detection at a site that did not see the local signal; when a re-check
  finds that only tracks begun after the change agree with what is measured now, it is
  the first detection of the oldest of those tracks.
  Report it as the start time. `resumed_ns` is the server time of the latest re-detection after a loss and
  `break_s` that loss in seconds. `recheck` is a plain sentence while a position or
  measurements from before a break, or from before a change of waveform class, level
  (6 dB) or width (twice and at least 1 kHz), are checked against new measurements;
  null otherwise. `received_lo_hz` / `received_hi_hz` span the downlink ranges of the
  `seen` evidence rows and `seen_site_ids` lists the sites with a `seen` row, in row
  order, so a local event can be shown by where it is received. `width_lower_bound`
  (also in `EventSummary`) is true when, in every monitor window holding the event, its
  detections together reach both edges of the window (within 8 % of the span, where the
  receiver's filter rolls off, and at least 1.5 bins): the signal may be wider than its
  range, so its width is a lower bound (the advisory then writes `(at least N kHz
  wide)` instead of `(about ...)`).
- `Transition.reason` is plain words: `all detections released`, `detected again after
  a break of <duration>` (within 30 s, with a position or rows, followed by `; the
  earlier position (or measurements) is checked against new measurements before it is
  kept`), `re-check: <why>`, `re-check passed: <why>` (`<why>` is `pair X measures the
  same time and frequency differences as before`, or, when no pair measured before the
  change came back within 3 rounds, `pair X agrees with its own measurements, and no pair
  measured before the change was measured again in 3 rounds, so the change could not be
  tested`), `another transmission: <why>;
  earlier position and measurements set aside`, `position withdrawn: measurements
  inconsistent (reduced chi-square N): ...`, `the position is not a fix: <reason>`,
  `position withdrawn: the position is not a fix: <reason>` (a located event whose new
  solution is not a fix), `position withdrawn: the newest measurements, added to the N it
  rested on, admit no position (<solver reason>)` (the published position, its residuals
  and `solution_t_ns` are cleared; the rows stay used; when some of its rows slid out of
  the solver window it reads `position withdrawn: the newest N measurements (the solver
  window), which hold every measurement since the position and K of the M it rested on,
  admit no position (<solver reason>)`, or `..., all newer than the M it rested on, ...`),
  `the measurements agree again`, `judged local: nothing to characterize or locate by
  satellite`, `position set aside: they belong to the local signal measured before`
  (when an uplink signal takes a local event's frequency and something had been
  measured),
  `closed by operator`, `merged into EV-000012`, `split from ...`, `server restarted: kept as
  history`, and the set-aside reasons of ARCHITECTURE.md section 7. A document keeps the
  first and the newest 200 transitions and 500 solution-history rows, and the newest
  1000 measurement rows (the store keeps all).
- An event is `located` only while its solution is a fix (`is_fix`): converged,
  consistent (below), with redundancy (`dof >= 1`: more measurements than the two
  unknowns of a position, so the fit can be checked against itself) and a 95 %
  semi-major axis within the server's sanity bound (1,000 km, or a quarter of the
  region's smaller extent when that is less: `events.fix_semi_major_limit_m`). A
  solution that is not a fix is still published, with `is_fix: false` and
  `not_fix_reason`, one plain clause: `the solver did not converge`, `the measurements
  do not fit one stationary transmitter (reduced chi-square X, limit Y)`, `no
  redundancy: N measurements for the two unknowns of a position, so the fit cannot be
  checked against itself`, `the 95 % error ellipse has a semi-major axis of N km,
  more than the M km a fix may have here: the measurements do not pin the position
  down`, or, for an event not yet located whose rows go through a satellite whose
  doubt is not yet confirmed (below), `the reference calibrations have just put the
  position of X wrong beyond its configured uncertainty, and the ellipse does not
  include that error yet (it is widened once a later round confirms the doubt)`; each
  ambiguity carries its own. A located event whose new solution is not a fix
  goes back to `locating` (the doubt reason never withdraws a located event). A fit is inconsistent when `reduced_chi2 > processing.solver.max_reduced_chi2` (5) and the
  chi-square tail probability is below 0.001: it is published with `fit_consistent:
  false`, `fit_p_value` and `ellipse_scale = sqrt(reduced_chi2)` already applied to
  `ellipse.semi_major_m`, `semi_minor_m` and `cep50_m` (its square to the three
  covariances), and a located event goes back to `locating`. Each ambiguity is judged on
  its own chi-square; `fit_p_value` is null when `dof < 1`. `SolutionSummary` and
  `solution_history` rows carry `fit_consistent` (their semi-axes are the enlarged
  ones) and `is_fix`; a history row has `set_aside: true` once its measurements were set aside (a
  break, another transmitter, a local signal). `dominant_error` (and the accuracy map's
  `dominant` grid) is one of `core.solver.ERROR_SOURCES`: `noise`,
  `ephemeris_position`, `ephemeris_velocity`, `clock`, `oscillator`, `tdoa_bias`,
  `reference`, `ionosphere`, `troposphere`.
- `SolutionOut.ephemeris_allowance` lists the satellites whose position sigma the solve
  widened because the reference calibrations put their position wrong beyond the
  configured uncertainty (MATH.md section 9, *ephemeris allowance*), sorted by `sat_id`;
  empty when the solve used the configured uncertainty unchanged, and always on an
  ambiguity. Each `EphemerisAllowanceOut` gives `scale`, the factor (greater than 1) on
  the satellite's configured radial / in-track / cross-track position sigma (a component
  configured as zero takes `processing.uncertainty.ephemeris.operator.pos_m` first),
  `sigma_ric_m`, the widened one-sigma in meters, and `capped`: the scale is the largest
  allowed (`processing.calibration.ephemeris_allowance_max_scale`, 20) and the
  calibrations ask for more, or they could not size the error, so the ellipse may still
  not cover it. The ellipse, CEP50 and covariances already include the allowance and
  `is_fix` judges the widened ellipse. `ellipse_widening` is, with an allowance, the 95 %
  semi-major axis over the one the same measurements give at the same position with the
  configured uncertainty (normally at least 1; 1.21: the major axis 21 % longer; the
  area grows more, both axes widening), null without an
  allowance or when it could not be worked out. Both are in the stored solution. A
  change of the list adds a transition: `ellipse widened: the reference calibrations put
  the position of X wrong beyond the configured uncertainty (position sigma taken
  larger: X N times[, at the largest allowed scale][; the 95 % ellipse's major axis P %
  longer than with the configured uncertainty])` (several satellites: `the positions of X and Y`,
  `X N1 times, Y N2 times`), `ellipse allowance for the position of X reduced to N times
  its configured sigma`, `ellipse allowance for the position of X removed: the configured
  uncertainty is used again` (several: `the positions of X and Y`), or `the allowance for
  the position of X is now|no longer at its largest scale`. `TaskingStatus.message` then
  ends `The ellipse is widened for the position of X (N times its configured sigma, about
  D), which the reference calibrations put wrong beyond the configured uncertainty:
  the 95 % ellipse's major axis is P % longer than with the configured uncertainty.` (D
  written as the advisory writes distances, `8.0 km`; with `, the largest scale allowed` and
  ` The allowance may still be too small: the transmitter may lie outside the ellipse.`
  for a capped one), and the advisory of a fix through X says `The ellipse is widened
  for the position of X, which the reference calibrations put wrong beyond its
  configured uncertainty (its position sigma taken N times larger, about D one sigma);
  check its ephemeris. Widened, the 95 % ellipse's major axis is P % longer than with
  the configured uncertainty.` (report action `...; the ellipse is widened for the
  ephemeris of X, which is in doubt`, or `the ephemerides of X and Y, which are in
  doubt`; a capped one says the calibrations put the position wrong by more than the
  largest allowance or by an amount they cannot size, and keeps `...; the ephemeris of X
  is in doubt, so the transmitter may lie outside this`). A growth of twice or more
  reads `N times as long as with the configured uncertainty`. Loading a configuration
  ends every allowance at once (the calibrations go with it). `Event.doubted_satellite_ids` lists every
  satellite the clock checks name when tasking last looked at the event (network-wide,
  sorted), so a console can tell those the published solve covers from those it does
  not cover yet.
- `EvidenceRow.sat_id` is null for an event outside every passband.
  `predicted_margin_db` is how far above the channel's threshold the signal should be
  given the strongest sighting (null when it cannot be predicted). `last_seen_ns` is
  the newest frame in which the channel's detection of the event was above threshold,
  also after the detection ended (so `stale` and `not_seen_capable` rows say how old
  the sighting is); null when the channel never saw the current transmission: once a
  transmission is set aside (a long break, another transmitter, a local signal taking
  over an uplink event) the sightings of channels holding no current detection are
  forgotten.
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
  while the next one runs; it also names a satellite whose orbit cannot be evaluated
  (`Satellite X: orbit not available (...); its pairs are not tasked.`), an
  inconsistent fit (`The measurements do not fit one stationary transmitter (reduced
  chi-square N, limit L): ...`) and a consistent solution that is not a fix (it ends
  `The position is shown but is not a fix: <not_fix_reason>.`). `solve_reason` says why the last solve gave no position
  (`unobservable`, `fewer than two accepted measurements`, ...). `unlocatable_reason` is
  set when the signal itself cannot be located, one of three sentences: every pair of
  the latest round was ambiguous (`... the waveform repeats itself ...`) or coupled
  (`... delay and Doppler cannot be told apart ...`) and no row has been used since, or
  fewer than two visible satellites relay the frequency (`Only one satellite relays
  this frequency (or can see the transmitter) ...`); it clears once a row is used.
- `Status.satellites` has one row per configured satellite, refreshed every 30 s and on
  a configuration change: `ephemeris_ok` false when its orbit cannot be evaluated at the
  server's time, with `unavailable_since_ns` (an ephemeris table's last sample, else the
  first failing check) and `message` (`satellite <name> (<id>): ephemeris not available
  since|until <ISO> (<reason>); its pairs are not tasked and nothing is located through
  it until the ephemeris is updated`).
- `Status.site_clocks` are per-site clock offsets fitted from the TDOA calibrations
  still valid, including the same-satellite clock checks tasking schedules (MATH.md
  section 9): `offset_us` is the site's labeled time minus true time in microseconds
  (negative: late), relative to the median of the sites whose clock differences from
  it the calibrations determine (else of its whole group);
  `sigma_us` the RMS residual (null with one correction). `caution` means a clock fault
  is likely or cannot be ruled out: `|offset| >= max(1 us, 10 x clock.sigma_ns, 3 x the
  site-dependent ephemeris allowance, the largest difference between satellites any
  site of its group shows)` (two sites whose difference alone is known are both
  flagged on their difference). `message` is a plain sentence or null. With `caution` it says either
  `... its clock is probably off (calibrations through X and Y agree, so it is not a
  satellite position error); check its timing receiver ...` or, with the site seen
  through fewer than two satellites, `... either its clock is off (check its timing
  receiver: GPS, PPS and 10 MHz reference), or the position of X is wrong beyond its
  configured uncertainty (seen from this site it would look the same); a calibration
  through a second satellite will tell them apart.` Without `caution` it may say that
  the site's timing differs between satellites (`... through X about N µs ahead; through
  Y ...`), so its clock is probably fine and the position of the satellite named is
  probably wrong beyond its configured uncertainty (check its ephemeris);
  `suspect_satellite_ids` lists the satellites that message names (empty otherwise);
  tasking copies them, every site's together, to each event it tasks
  (`Event.doubted_satellite_ids`) and, once a satellite has been named in two rounds
  at least a capture interval apart, widens its position sigma in the live solves
  (`SolutionOut.ephemeris_allowance`, below). Until then a solution of an event not yet
  located whose rows go through it is not a fix (`not_fix_reason` above). A located
  event whose fix rests on rows through a doubted satellite that its published solve
  did not widen for (yet) gets an advisory saying the ellipse does not include that
  error yet and the transmitter may lie outside it (the report action adds `; the
  ephemeris of X is in doubt, so the transmitter may lie outside this`, or `the
  ephemerides of X and Y are in doubt`).
- `ChannelStatus.windows` gives each monitor window's state: `last_frame_age_s` (from
  the hub), `detection_frame_age_s` (the newest frame detection accepted), `blind`
  (frames arrive but none has been accepted for 10 frame periods) with `blind_reason`,
  `learning` (fewer than half the tracker's bins on their own history yet: it still
  works from its start-up floor), `floor_raised_db` (the window's
  noise floor is more than `detection.threshold_db` above the lowest of the channel's
  other windows of the same span: a signal may fill the window) and `note`.
  `detection_problem` summarizes them in words. `ok` is false while the node is online
  and a window is blind. These fields are null or false without detection data and for
  an offline node.
- `AccuracyRequest.t` defaults to the start of the current minute (so a repeated
  request is answered from the cache of 8 maps). `uncertainty` replaces
  `processing.uncertainty` for that map only (its `iono_vtec_tecu`, `iono_correlation`,
  `tropo_zenith_m` and `tropo_correlation` default to 30, 0.5, 2.4 and 0.9 when left
  out, whatever the configuration says); satellites with their own `ephemeris_sigma`
  keep it and site clocks keep `Site.clock.sigma_ns`. Pair ends are channel ids, or
  `channel@satellite` for a channel watching several satellites (422 at `pairs.i.j` for
  an unknown channel, a satellite the channel does not watch or a malformed end, such as
  an empty part or a second `@`; at `pairs.i` when both ends are one channel or the
  pair repeats an earlier one, in either order and however its ends are spelled: a bare
  channel id and that channel `@` the satellite it correlates on are one end). More than
  128 pairs is a 422 at
  `pairs` from request validation. `reference_carrier_id` is a reference carrier's id,
  or `"nearest"`: each cell's pairs are then calibrated against the reference carrier
  tasking would use for an emitter in that cell (relayed by both transponders of the
  pair, clear of the emitter's band, nearest the cell, then nearest in frequency; cells
  with none stay uncalibrated; a configured carrier named `nearest` takes precedence;
  422 when the configuration has no reference carrier). One map is
  computed at a time, up to 4 wait; the grid is computed in latitude bands of about 256
  cells, fewer when many pairs would need more than 256 MiB per band (never under two
  grid rows), and the 120 s limit is checked between bands.
- `AccuracyGrid.pairs` has one `AccuracyPairSummary` per requested pair, in request
  order, merged over the latitude bands (the command line's JSON carries the same).
  "Cells" are grid cells, admissible or not. `a_channel`, `b_channel` are the ends as
  listed; `a_sat_id`, `b_sat_id` the satellites they correlate on (null when the pair
  cannot be resolved). `calibrated` lists the kinds calibrated in at least one cell.
  `reference_carrier_id` is the carrier the request named (null for none and for
  `"nearest"`). `reference_cells` counts the cells by the reference applied, summed over
  the bands: for `"nearest"`, carrier id -> cells for which it is the nearest eligible
  carrier, and `"none"` -> cells with no eligible carrier (none clear of the target
  band and relayed by both transponders); for a named carrier, `{id: cells}` where it
  calibrates the pair and `{"none": cells}` where it does not; `{}` without a reference.
  `uncalibrated_cells` counts the cells in which no reference calibrates the pair: the
  `"none"` cells, plus the cells of a nearest carrier that cannot calibrate it (all
  cells without a reference). `reference_reason` says why, null when the reference
  calibrates every cell (or none was requested, or the pair cannot be resolved): the
  engine's reason for a carrier (for example `reference 'ref-x': predicted
  post-correlation SNR -48.0 dB below threshold`), and for `"nearest"` cells with no
  eligible carrier `no reference carrier clear of the target band is relayed by both
  transponders (<carrier> overlaps the target band; <carrier> is not relayed by both
  <sat>/<transponder> and <sat>/<transponder>)`; several causes are joined with `; `,
  each with its cell count, the largest first. `reason` is why the pair gives nothing:
  the first band's, with a note per distinct reason naming its latitudes when the
  bands differ. `notes` holds each band note once, in first-seen order; one that not
  every band gives starts with its latitudes (`latitudes -30 to -21: ...`); the
  engine's `not calibrated: <reason>` notes are left out (the reason is in
  `reference_reason`); a named carrier whose band overlaps the target band adds `<id>
  overlaps the target band, so tasking would not calibrate against it; this map assumes
  it does` to each pair it calibrates (the engine calibrates against a named carrier
  wherever it lies). Whether a carrier calibrates a pair does not depend on the cell;
  for `"nearest"` the engine reports the reference of each band's most common choice
  only, so when no band shows a chosen carrier's outcome the server computes a one-cell
  map against that carrier to learn it.
- `LinkBudgetRow` exists only for paths that exist (satellite above both horizons,
  frequency inside the transponder, an orbit that can be evaluated at `t`). `snr_db` is
  in the emitter's bandwidth (1 Hz for
  zero width); `detectable` is `10 log10(1 + C / (N0 max(bandwidth, bin))) >=
  processing.detection.threshold_db` in the bins of the monitor covering `f_down_hz`
  (`monitor_id`; null and not detectable when no window covers it).
- `SimTruth.events` covers the active events. Truth is fictional and labeled
  simulated wherever it is shown.

## WebSocket `/ws/ui`

Server to browser, text frames:

- `status {type, server_t_ns, config_version, marking, simulation, nodes: [NodeStatus], events_active, queue, satellites, site_clocks}` about once a second (a new client gets the latest one at once)
- `event {type, event: Event}` (the whole object on every published change)
- `config {type, config_version}` when configuration is replaced
- `sim {type, version}` when the scenario changes

An event is published at once on a material change (state, verdict, evidence states,
satellites, classification, a measurement, a solution, a correlation surface, a
re-check, its onset, whether its width is a lower bound) and at most every 5 s for
numeric drift (SNR, C/N0, margins, edges).

Binary frames reuse the `PLB1` framing with header
`{type: "psd", channel_id, monitor_id, t_ns: "<string>", center_hz, span_hz, nfft, seq,
has_baseline, detections: [{track_id, f_lo_hz, f_hi_hz, authorized_carrier_id, anomaly}]}`
(`authorized_carrier_id` is written as null, not omitted) and payload `float32[nfft]`
PSD followed by `float32[nfft]` baseline when `has_baseline` (false until the tracker
has learned a baseline, since a NaN payload is never sent).

Browser to server: `subscribe {type, psd: [[channel_id, monitor_id], ...]}` (at most
256 windows) replaces the streamed selection. Other messages, and a `subscribe` holding
text that is not valid Unicode, are ignored (the first three logged). Each client has a
mailbox in which a newer message of the same kind
(status, config, sim, one event id, one spectrum window) replaces an undelivered older
one, so a slow browser only loses its own intermediate frames and never blocks the
pipeline or other clients.

Close codes: 1008 for a missing or wrong `?token=` and for a foreign `Origin` (the
handshake is accepted and closed at once so the browser learns why; a `Host` the server
does not answer to is refused before the handshake), 1013 while the server is starting
or when a frame
has taken longer than 30 s to leave (`ws.SEND_TIMEOUT_S`; a very slow link receiving a
large event may need more), 1001 at shutdown and for handshakes during shutdown, 1011
for an internal error, 1000 when the browser leaves.
