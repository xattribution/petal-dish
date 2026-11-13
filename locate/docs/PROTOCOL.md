# PETAL Locate — node to server protocol (version 1)

Transport: one WebSocket per node, `ws(s)://server/ws/node`. Control messages are text
frames holding one JSON object with a `type` field. Sample and spectrum data are binary
frames. The node always dials out, so sites behind NAT need no inbound ports. Production
deployments terminate TLS in front of the server (`wss`); the token below is a bearer
secret and must not travel in clear text outside a lab.

All absolute times are integer UTC nanoseconds (`*_ns`). They exceed 2^53, so
JavaScript consumers must parse them as BigInt or strings; the node and server are
Python and use arbitrary-precision ints.

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
   `{type, protocol: 1, node_id, site_id, token, software, driver, capabilities:
   {min_hz, max_hz, max_sample_rate_hz, lookback_s}, clock: ClockStatus, t_ns}`
2. Server -> `welcome`
   `{type, protocol: 1, server_t_ns, heartbeat_s, config_version, channels:
   [ChannelAssignment], simulation: {enabled, scenario_url?}}`
   or `reject {type, reason}` followed by close (bad token, unknown site, protocol
   mismatch, duplicate node for the site).
3. Node -> `heartbeat {type, seq, t_ns, clock: ClockStatus, load: {cpu, queue}}`
   every `heartbeat_s`; server -> `heartbeat_ack {type, seq, server_t_ns}`.
   Three missed heartbeats mark the node offline and fail its pending captures.
4. Server -> `assign {type, config_version, channels: [ChannelAssignment]}` whenever
   configuration changes; node -> `assign_ack {type, config_version, ok, error?}`.

```
ChannelAssignment  {channel_id, antenna_summary, monitors: [{monitor_id, center_hz, span_hz, nfft, navg, rate_hz}]}
ClockStatus        {source: "gpsdo" | "ntp" | "sim" | "free", locked: bool, est_offset_ns: number, holdover_s: number}
```

## Spectrum frames (node -> server, binary)

Header `{type: "psd", channel_id, monitor_id, t_ns, center_hz, span_hz, nfft, navg,
seq, units: "dB"}`; payload `float32[nfft]`, fft-shifted so bin `k` is at
`center_hz + (k - nfft/2) * span_hz / nfft`.

## Captures

Server -> `capture_request`
`{type, capture_id, channel_id, t_start_ns, n_samples, sample_rate_hz, center_hz,
deadline_ns}`

`t_start_ns` is normally a little in the future. It may be in the past when the driver
advertises `lookback_s` and still holds that interval.

Node -> one or more binary frames, header
`{type: "capture_data", capture_id, channel_id, seq, last: bool, t_first_ns,
sample_rate_hz, center_hz, n_samples_total, offset, n, overflow: bool, clock:
ClockStatus}`; payload `complex64[n]` (interleaved float32 I, Q). Chunks are at most
4 MiB of payload and arrive in order; `offset` is the index of the chunk's first
sample.

Node -> `capture_error {type, capture_id, channel_id, code, message}` with codes
`out_of_range` (frequency or rate), `too_late` (interval no longer buffered),
`overflow`, `driver`, `busy`, `cancelled`.

Server -> `capture_cancel {type, capture_id}` is advisory.

The node reports the true time of its first sample in `t_first_ns`; it need not equal
`t_start_ns` exactly. The correlator corrects for the difference.

## Errors and limits

- Unknown `type`: ignored with a logged warning (forward compatibility).
- Malformed message: `error {type, code: "bad_message", detail}`; three in a row
  close the session.
- Server enforces per-node limits: at most 8 concurrent captures, 64 MiB per capture.
- Either side may close with code 1001 for shutdown; nodes reconnect with exponential
  backoff (1 s doubling to 30 s, plus up to 20 % jitter) and resume from `hello`.

## Simulation backplane

When the server runs with a scenario, `welcome.simulation.enabled` is true and
simulated nodes fetch the scenario and configuration from `scenario_url` over HTTP
(with the same bearer token) and poll its version. This channel exists only to drive
simulated radios; real drivers never use it.
