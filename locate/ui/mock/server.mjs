// Mock of the PETAL Locate console interface (PROTOCOL.md section B) for development and
// the smoke test, served from the fixtures that mock/generate_fixtures.py computes with
// the real numeric core. Used by the Vite dev server (mock/vite-plugin.mjs) and by the
// standalone server (mock/serve.mjs).
//
// What is live: status every second (latency jitter, frame ages, Manila's holdover
// counting up, Hilo offline), spectrum frames at the monitors' 2 Hz for every subscribed
// window (Gamma(navg) scatter of the expected PSD, slow drift, detections from the
// expected excess over the baseline), event updates every 5 s for active events,
// config and scenario versions with ETag / If-Match and a 409 path.
//
// Fixture times are at T0; every *_ns string is shifted by (mock start - T0) on the way
// out, so the data ages from the moment the mock starts. Geometry is the T0 geometry.
// Spectrum reflects the precomputed emitters only: switching one off removes it, an EIRP
// change scales it (linear transponder), a frequency change shifts it; moving an emitter
// or adding a new one does not change the spectrum (the mock cannot run the simulator).
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { WebSocketServer } from 'ws';
import { validateConfig, configWarnings, validateScenario, fillDerivedMonitors } from './validate.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const FIX = join(here, 'fixtures');
const load = (name) => JSON.parse(readFileSync(join(FIX, name), 'utf8'));
const clone = (v) => structuredClone(v);

export function createMock(options = {}) {
  const token = options.token ?? process.env.MOCK_TOKEN ?? null;
  const log = options.log ?? (() => {});
  const fx = {
    meta: load('meta.json'),
    config: load('config.json'),
    schema: load('config_schema.json'),
    status: load('status.json'),
    events: load('events.json'),
    lop: load('lop.json'),
    geometry: load('geometry.json'),
    spectrum: load('spectrum.json'),
    scenario: load('scenario.json'),
    presets: load('presets.json'),
    truth: load('sim_truth.json'),
    linkBudget: load('link_budget.json'),
    accuracy: (() => {
      try {
        return load('accuracy.json');
      } catch {
        return { variants: [] };
      }
    })(),
  };
  const T0 = BigInt(fx.meta.server_t_ns);
  const startMs = Date.now();
  const SHIFT = BigInt(startMs) * 1_000_000n - T0;
  const nowNs = () => BigInt(Date.now()) * 1_000_000n;
  /** fixture-time (T0-based) now */
  const fixNow = () => nowNs() - SHIFT;

  const state = {
    configVersion: fx.config.version,
    config: clone(fx.config.config),
    derived: [...(fx.config.derived_monitors ?? [])],
    scenarioVersion: fx.scenario.version,
    scenario: clone(fx.scenario.scenario),
    events: new Map(fx.events.map((e) => [e.id, clone(e)])),
    clients: new Set(),
  };

  // ------------------------------------------------------------------ helpers
  const shiftIso = (iso) => new Date(Date.parse(iso) + Number(SHIFT / 1_000_000n)).toISOString().replace(/\.\d{3}Z$/, 'Z');
  function shift(value, key = '') {
    if (Array.isArray(value)) return value.map((v) => shift(v, key));
    if (value && typeof value === 'object') {
      const out = {};
      for (const [k, v] of Object.entries(value)) out[k] = shift(v, k);
      return out;
    }
    if (typeof value === 'string' && key.endsWith('_ns') && /^\d+$/.test(value)) return (BigInt(value) + SHIFT).toString();
    if (typeof value === 'string' && key === 'connected_since') return shiftIso(value);
    return value;
  }

  function send(res, status, body, headers = {}) {
    const text = body === undefined ? '' : JSON.stringify(body);
    res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store', ...headers });
    res.end(text);
  }

  async function readBody(req) {
    const chunks = [];
    for await (const c of req) chunks.push(c);
    const text = Buffer.concat(chunks).toString('utf8');
    if (!text) return undefined;
    try {
      return JSON.parse(text);
    } catch {
      return Symbol.for('bad-json');
    }
  }

  function authorized(req, url) {
    if (!token) return true;
    const h = req.headers.authorization ?? '';
    return h === `Bearer ${token}` || url.searchParams.get('token') === token;
  }

  function broadcast(obj) {
    const text = JSON.stringify(obj);
    for (const c of state.clients) if (c.ws.readyState === 1) c.ws.send(text);
  }

  // ------------------------------------------------------------------ live status
  const rnd = (a, b) => a + Math.random() * (b - a);
  function liveStatus() {
    const s = clone(fx.status);
    const elapsed = (Date.now() - startMs) / 1000;
    s.server_t_ns = nowNs().toString();
    s.config_version = state.configVersion;
    s.events_active = [...state.events.values()].filter((e) => !['lost', 'closed'].includes(e.state)).length;
    s.queue.captures_pending = Math.random() < 0.5 ? 2 : 1;
    for (const n of s.nodes) {
      if (n.online) {
        n.latency_ms = Math.round((n.latency_ms ?? 50) * rnd(0.92, 1.12) * 10) / 10;
        for (const ch of n.channels) {
          ch.last_frame_age_s = Math.round(rnd(0.05, 0.55) * 100) / 100;
          for (const w of ch.windows ?? []) w.last_frame_age_s = w.detection_frame_age_s = Math.round(rnd(0.05, 0.55) * 100) / 100;
        }
        if (n.clock && !n.clock.locked) {
          n.clock.holdover_s = Math.round(n.clock.holdover_s + elapsed);
          n.clock.est_offset_ns = Math.round(n.clock.est_offset_ns + elapsed * 0.9);
        }
        n.connected_since = shiftIso(n.connected_since);
      } else {
        for (const ch of n.channels) {
          ch.last_frame_age_s = Math.round((ch.last_frame_age_s ?? 0) + elapsed);
          for (const w of ch.windows ?? []) w.last_frame_age_s = ch.last_frame_age_s;
        }
      }
    }
    return s;
  }

  // ------------------------------------------------------------------ events
  function summary(e) {
    const sol = e.solution;
    return {
      id: e.id, state: e.state, verdict: e.verdict, first_seen_ns: e.first_seen_ns, last_seen_ns: e.last_seen_ns,
      uplink_lo_hz: e.uplink_lo_hz, uplink_hi_hz: e.uplink_hi_hz, satellite_ids: e.satellite_ids,
      label: e.classification ? e.classification.label : null, headline: e.advisory.headline,
      solution: sol ? { lat_deg: sol.lat_deg, lon_deg: sol.lon_deg, semi_major_m: sol.ellipse.semi_major_m, semi_minor_m: sol.ellipse.semi_minor_m,
        azimuth_deg: sol.ellipse.azimuth_deg, cep50_m: sol.cep50_m, ambiguous: sol.ambiguities.length > 0,
        // as server/pipeline/events.EventBook.summary: every other position that fits as well
        ambiguities: sol.ambiguities.map((a) => ({ lat_deg: a.lat_deg, lon_deg: a.lon_deg, semi_major_m: a.ellipse.semi_major_m,
          semi_minor_m: a.ellipse.semi_minor_m, azimuth_deg: a.ellipse.azimuth_deg })),
        fit_consistent: sol.fit_consistent ?? true } : null,
      simulated: e.simulated ?? false,
      onset_ns: e.onset_ns ?? null, received_lo_hz: e.received_lo_hz ?? null, received_hi_hz: e.received_hi_hz ?? null, seen_site_ids: e.seen_site_ids ?? [],
    };
  }

  function touchActive(announce = true) {
    // active events keep being detected: last seen is always within the last half second
    const t = (fixNow() - 300_000_000n).toString();
    for (const e of state.events.values()) {
      if (['lost', 'closed'].includes(e.state)) continue;
      e.last_seen_ns = t;
      for (const r of e.evidence) if (r.state === 'seen') r.last_seen_ns = t;
      // tasking keeps its rhythm: the next round is always ahead (the fixture does not add measurements)
      const tk = e.tasking;
      if (tk && tk.state === 'waiting' && tk.next_round_ns && tk.last_round_ns) {
        const step = BigInt(tk.next_round_ns) - BigInt(tk.last_round_ns) || 5_000_000_000n;
        let next = BigInt(tk.next_round_ns);
        while (next < fixNow()) next += step;
        tk.next_round_ns = next.toString();
        tk.last_round_ns = (next - step).toString();
      }
      if (announce) broadcast({ type: 'event', event: shift(e) });
    }
  }

  // ------------------------------------------------------------------ spectrum
  const fromB64 = (b64) => {
    const b = Buffer.from(b64, 'base64');
    return new Float32Array(b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength));
  };
  const windows = new Map();
  for (const w of fx.spectrum.windows) {
    windows.set(`${w.channel_id}\u0000${w.monitor_id}`, {
      ...w,
      base: fromB64(w.base_lin),
      parts: w.parts.map((p) => ({ ...p, lin: fromB64(p.lin) })),
      seq: 0,
      phase: Math.random() * Math.PI * 2,
    });
  }
  const offlineSites = new Set(fx.status.nodes.filter((n) => !n.online).map((n) => n.site_id));
  const fixtureEmitters = new Map(fx.scenario.scenario.emitters.map((e) => [e.id, e]));
  let trackIds = new Map();
  let nextTrack = 100;

  function emitterAdjust(id) {
    const now = state.scenario.emitters.find((e) => e.id === id);
    const orig = fixtureEmitters.get(id);
    if (!now || !orig || now.enabled === false) return null;
    return { gain: 10 ** (((now.eirp_dbw ?? orig.eirp_dbw) - orig.eirp_dbw) / 10), dfHz: (now.freq_hz ?? orig.freq_hz) - orig.freq_hz };
  }

  function gammaNavg(navg) {
    let s = 0;
    for (let i = 0; i < navg; i++) s -= Math.log(1 - Math.random());
    return s / navg;
  }

  function encodeFrame(header, psd, baseline) {
    let json = JSON.stringify(header);
    const pad = (8 - ((8 + Buffer.byteLength(json)) % 8)) % 8;
    json += ' '.repeat(pad);
    const hb = Buffer.from(json, 'utf8');
    const out = Buffer.alloc(8 + hb.length + 4 * psd.length * (baseline ? 2 : 1));
    out.write('PLB1', 0, 'latin1');
    out.writeUInt32LE(hb.length, 4);
    hb.copy(out, 8);
    let o = 8 + hb.length;
    for (const v of psd) { out.writeFloatLE(v, o); o += 4; }
    if (baseline) for (const v of baseline) { out.writeFloatLE(v, o); o += 4; }
    return out;
  }

  function frameFor(key) {
    const w = windows.get(key);
    if (!w || offlineSites.has(w.site_id)) return null;
    const n = w.nfft;
    const expected = Float64Array.from(w.base);
    const binHz = w.span_hz / n;
    const contributions = [];
    for (const p of w.parts) {
      const adj = emitterAdjust(p.emitter_id);
      if (!adj) continue;
      const shiftBins = Math.round(adj.dfHz / binHz);
      // slow power fluctuation of the emitter, +/- 0.7 dB
      const g = adj.gain * 10 ** ((0.7 * Math.sin(Date.now() / 9000 + w.phase)) / 10);
      let lo = n, hi = -1;
      for (let i = 0; i < p.lin.length; i++) {
        const k = p.start + i + shiftBins;
        if (k < 0 || k >= n) continue;
        expected[k] += p.lin[i] * g;
        if (p.lin[i] * g > 0) { lo = Math.min(lo, k); hi = Math.max(hi, k); }
      }
      if (hi >= lo) contributions.push({ id: p.emitter_id, lo, hi });
    }
    // drifting floor: +/- 0.25 dB over a few minutes
    const drift = 10 ** ((0.25 * Math.sin(Date.now() / 47000 + w.phase)) / 10);
    const psd = new Float32Array(n);
    const baseline = new Float32Array(n);
    for (let k = 0; k < n; k++) {
      psd[k] = 10 * Math.log10(expected[k] * drift * gammaNavg(w.navg));
      baseline[k] = 10 * Math.log10(w.base[k]);
    }
    // detections: contiguous runs where the expected PSD exceeds the baseline by the threshold
    const thr = 10 ** (fx.spectrum.threshold_db / 10);
    const detections = [];
    let k = 0;
    while (k < n) {
      if (expected[k] / w.base[k] >= thr) {
        const start = k;
        while (k < n && expected[k] / w.base[k] >= thr * 0.5) k++;
        const f0 = w.center_hz + (start - n / 2) * binHz;
        const f1 = w.center_hz + (k - n / 2) * binHz;
        const who = contributions.find((c) => c.lo <= k && c.hi >= start)?.id ?? 'unknown';
        const tk = `${key}\u0000${who}`;
        if (!trackIds.has(tk)) trackIds.set(tk, nextTrack++);
        detections.push({ track_id: trackIds.get(tk), f_lo_hz: f0, f_hi_hz: f1, authorized_carrier_id: null, anomaly: false });
      } else k++;
    }
    w.seq += 1;
    const header = {
      type: 'psd', channel_id: w.channel_id, monitor_id: w.monitor_id, t_ns: nowNs().toString(), center_hz: w.center_hz,
      span_hz: w.span_hz, nfft: n, seq: w.seq, has_baseline: true, detections,
    };
    return encodeFrame(header, psd, baseline);
  }

  // ------------------------------------------------------------------ geometry and analysis
  function subpointsAt(tIso) {
    const series = fx.geometry.subpoints;
    let offset = 0;
    if (tIso) {
      const tMs = Date.parse(tIso);
      if (Number.isFinite(tMs)) offset = (tMs - startMs) / 1000;
    } else offset = (Date.now() - startMs) / 1000;
    const day = 86164;
    const o = ((offset % day) + day) % day;
    let best = series[0];
    for (const s of series) if (Math.abs(s.offset_s - o) < Math.abs(best.offset_s - o)) best = s;
    return best.subpoints;
  }

  /** The server's 422 body (schemas.FieldErrorResponse): {detail, errors: [{loc, msg}], warnings}. */
  function fieldError(what, errors, warnings = []) {
    const first = errors[0];
    const more = errors.length > 1 ? ` (and ${errors.length - 1} more)` : '';
    const where = first && first.loc && first.loc !== '(root)' ? `${first.loc}: ` : '';
    return { detail: first ? `the ${what} does not validate: ${where}${first.msg}${more}` : `the ${what} does not validate`, errors, warnings };
  }

  function accuracyFor(body) {
    const key = (pairs, kinds, ref) => JSON.stringify([pairs.map((p) => p.join('|')).sort(), [...kinds].sort(), ref ?? null]);
    const want = key(body.pairs ?? [], body.kinds ?? ['tdoa', 'fdoa'], body.reference_carrier_id);
    const hit = fx.accuracy.variants.find((v) => key(v.pairs, v.kinds, v.reference_carrier_id) === want);
    if (hit) return { ok: true, grid: hit.grid };
    const have = fx.accuracy.variants.map((v) => `${v.pairs.map((p) => p.join(' / ')).join(' + ')} (${v.kinds.join('+')}${v.reference_carrier_id ? `, ref ${v.reference_carrier_id}` : ''})`);
    return {
      ok: false,
      body: fieldError('accuracy request', [{ loc: 'pairs', msg: `The mock server has precomputed grids only for: ${[...new Set(have)].join('; ') || 'none (run mock/generate_fixtures.py)'}` }]),
    };
  }

  function truthNow() {
    const t = clone(fx.truth);
    t.version = state.scenarioVersion;
    const byId = new Map(t.emitters.map((e) => [e.id, e]));
    const out = [];
    for (const e of state.scenario.emitters) {
      const base = byId.get(e.id);
      const wf = e.waveform ?? { type: 'noise', bandwidth_hz: 0 };
      out.push({
        id: e.id, name: e.name || e.id, kind: e.kind, lat_deg: e.lat_deg ?? base?.lat_deg ?? 0, lon_deg: e.lon_deg ?? base?.lon_deg ?? 0,
        freq_hz: e.freq_hz ?? base?.freq_hz ?? 0, eirp_dbw: e.eirp_dbw ?? base?.eirp_dbw ?? 0,
        bandwidth_hz: wf.type === 'cw' ? 0 : wf.type === 'psk' ? (wf.bandwidth_hz || (wf.symbol_rate_hz ?? 0) * (1 + (wf.rolloff ?? 0.35))) : (wf.bandwidth_hz ?? 0),
        waveform: wf.type, enabled: e.enabled !== false, active: e.enabled !== false,
      });
    }
    for (const e of t.emitters) if (e.kind === 'carrier') out.push(e);
    t.emitters = out;
    return t;
  }

  function setScenario(s) {
    state.scenario = s;
    state.scenarioVersion += 1;
    broadcast({ type: 'sim', version: state.scenarioVersion });
    return { version: state.scenarioVersion, scenario: state.scenario };
  }

  // ------------------------------------------------------------------ REST
  async function handle(req, res) {
    const url = new URL(req.url ?? '/', 'http://mock');
    const path = url.pathname;
    if (path.startsWith('/__mock/')) {
      if (path === '/__mock/bump-config' && req.method === 'POST') {
        // another operator saves a configuration: the next PUT with the old ETag gets 409
        state.configVersion += 1;
        state.config = { ...state.config, version: state.configVersion, description: `${state.config.description ?? ''}`.trim() };
        broadcast({ type: 'config', config_version: state.configVersion });
        send(res, 200, { version: state.configVersion });
        return true;
      }
      send(res, 404, { detail: 'unknown mock control' });
      return true;
    }
    if (!path.startsWith('/api/v1/')) return false;
    const p = path.slice('/api/v1'.length);
    const m = req.method ?? 'GET';
    if (p !== '/meta' && !authorized(req, url)) {
      send(res, 401, { detail: 'an API token is required (Authorization: Bearer <token>)' });
      return true;
    }
    log(`${m} ${p}`);
    let match;
    try {
      if (m === 'GET' && p === '/meta') {
        return send(res, 200, { ...fx.meta, server_t_ns: nowNs().toString(), api_token_required: Boolean(token) }), true;
      }
      if (p === '/config' && m === 'GET') {
        return send(res, 200, { version: state.configVersion, config: state.config, warnings: configWarnings(state.config), derived_monitors: state.derived }, { ETag: `"${state.configVersion}"` }), true;
      }
      if (p === '/config' && m === 'PUT') {
        const ifMatch = req.headers['if-match'];
        const body = await readBody(req);
        if (!ifMatch) return send(res, 428, { detail: 'If-Match is required: send the ETag of the configuration you edited' }), true;
        const v = Number(String(ifMatch).replace(/[^0-9]/g, ''));
        if (v !== state.configVersion) {
          return send(res, 409, { detail: `version ${v} is not the current configuration (version ${state.configVersion} was saved since)`, current_version: state.configVersion }, { ETag: `"${state.configVersion}"` }), true;
        }
        if (typeof body === 'symbol' || body === undefined) return send(res, 400, { detail: 'the body is not JSON' }), true;
        const result = validateConfig(body, fx.schema);
        if (!result.ok) {
          return send(res, 422, { detail: `${result.errors.length} field problem${result.errors.length === 1 ? '' : 's'}`, errors: result.errors, warnings: result.warnings }), true;
        }
        const filled = fillDerivedMonitors(body);
        state.configVersion += 1;
        state.config = { ...filled.config, version: state.configVersion };
        state.derived = filled.derived;
        broadcast({ type: 'config', config_version: state.configVersion });
        return send(res, 200, { version: state.configVersion, config: state.config, warnings: result.warnings, derived_monitors: state.derived }, { ETag: `"${state.configVersion}"` }), true;
      }
      if (p === '/config/validate' && m === 'POST') {
        const body = await readBody(req);
        if (typeof body === 'symbol' || body === undefined) return send(res, 400, { detail: 'the body is not JSON' }), true;
        return send(res, 200, validateConfig(body, fx.schema)), true;
      }
      if (p === '/config/schema' && m === 'GET') return send(res, 200, fx.schema), true;
      if (p === '/status' && m === 'GET') {
        const s = liveStatus();
        return send(res, 200, { server_t_ns: s.server_t_ns, config_version: s.config_version, nodes: s.nodes, events_active: s.events_active, queue: s.queue,
          satellites: s.satellites ?? [], site_clocks: s.site_clocks ?? [] }), true;
      }
      if (p.startsWith('/events') && m === 'GET') touchActive(false);
      if (p === '/events' && m === 'GET') {
        const st = url.searchParams.get('state');
        const limit = Number(url.searchParams.get('limit') ?? 100);
        const states = ['active', 'characterizing', 'closed', 'detected', 'located', 'locating', 'lost'];
        if (st !== null && !states.includes(st)) {
          const msg = `state: must be one of ${JSON.stringify(states).replace(/"/g, "'").replace(/,/g, ', ')}, got '${st}'`;
          return send(res, 422, { detail: msg, errors: [{ loc: 'state', msg }], warnings: [] }), true;
        }
        if (!(Number.isInteger(limit) && limit >= 1 && limit <= 1000)) {
          return send(res, 422, fieldError('request', [{ loc: 'limit', msg: 'Input should be less than or equal to 1000' }])), true;
        }
        let list = [...state.events.values()];
        if (st === 'active') list = list.filter((e) => !['lost', 'closed'].includes(e.state));
        else if (st) list = list.filter((e) => e.state === st);
        list.sort((a, b) => (BigInt(b.last_seen_ns) > BigInt(a.last_seen_ns) ? 1 : -1));
        return send(res, 200, shift(list.slice(0, limit).map(summary))), true;
      }
      if ((match = p.match(/^\/events\/([^/]+)$/)) && m === 'GET') {
        const e = state.events.get(decodeURIComponent(match[1]));
        return e ? send(res, 200, shift(e)) : send(res, 404, { detail: `no event ${match[1]}` }), true;
      }
      if ((match = p.match(/^\/events\/([^/]+)\/close$/)) && m === 'POST') {
        const e = state.events.get(decodeURIComponent(match[1]));
        if (!e) return send(res, 404, { detail: `no event ${match[1]}` }), true;
        if (e.state !== 'closed') {
          e.state = 'closed';
          e.transitions.push({ t_ns: fixNow().toString(), state: 'closed', verdict: e.verdict, reason: 'closed by operator' });
          broadcast({ type: 'event', event: shift(e) });
        }
        return send(res, 200, shift(e)), true;
      }
      if ((match = p.match(/^\/events\/([^/]+)\/lop$/)) && m === 'GET') {
        const id = decodeURIComponent(match[1]);
        if (!state.events.has(id)) return send(res, 404, { detail: `no event ${id}` }), true;
        return send(res, 200, shift(fx.lop[id] ?? { type: 'FeatureCollection', features: [] })), true;
      }
      if (p === '/geometry/subpoints' && m === 'GET') return send(res, 200, subpointsAt(url.searchParams.get('t'))), true;
      if (p === '/geometry/footprints' && m === 'GET') {
        const el = Number(url.searchParams.get('min_elevation_deg') ?? 0);
        const keys = Object.keys(fx.geometry.footprints).map(Number);
        const k = keys.reduce((a, b) => (Math.abs(b - el) < Math.abs(a - el) ? b : a), keys[0]);
        return send(res, 200, fx.geometry.footprints[String(k)]), true;
      }
      if (p === '/geometry/look-angles' && m === 'GET') return send(res, 200, fx.geometry.look_angles), true;
      if (p === '/analysis/accuracy' && m === 'POST') {
        const body = await readBody(req);
        if (!body || typeof body !== 'object' || !Array.isArray(body.pairs) || body.pairs.length === 0) {
          return send(res, 422, fieldError('request', [{ loc: 'pairs', msg: 'List should have at least 1 item after validation, not 0' }])), true;
        }
        if (body.uncertainty) {
          // the real server recomputes the map with the what-if uncertainty; the mock only has fixed grids
          return send(res, 422, fieldError('accuracy request', [{ loc: 'uncertainty', msg: 'The mock server has no grids for a what-if uncertainty; the real server computes them' }])), true;
        }
        const r = accuracyFor(body);
        await new Promise((ok) => setTimeout(ok, 600)); // the real computation takes a while
        return r.ok ? send(res, 200, shift(r.grid)) : send(res, 422, r.body), true;
      }
      if (p === '/analysis/link-budget' && m === 'POST') {
        const body = await readBody(req);
        const hit = Object.entries(fx.linkBudget).find(([id]) => {
          const e = fixtureEmitters.get(id);
          return e && body && Math.abs(e.lat_deg - body.lat_deg) < 0.05 && Math.abs(e.lon_deg - body.lon_deg) < 0.05 && Math.abs(e.freq_hz - body.f_e_hz) < 1000;
        });
        if (!hit) {
          return send(res, 422, fieldError('request', [{ loc: '(root)', msg: 'The mock server has link budgets only for the scenario emitters at their original positions and frequencies' }])), true;
        }
        return send(res, 200, shift(hit[1])), true;
      }
      if (p === '/sim/scenario' && m === 'GET') return send(res, 200, { version: state.scenarioVersion, scenario: state.scenario }), true;
      if (p === '/sim/scenario' && m === 'PUT') {
        const body = await readBody(req);
        if (typeof body === 'symbol' || !body) return send(res, 400, { detail: 'the body is not JSON' }), true;
        const problems = validateScenario(body, state.config);
        if (problems.length) return send(res, 422, fieldError('scenario', problems)), true;
        return send(res, 200, setScenario(body)), true;
      }
      if (p === '/sim/presets' && m === 'GET') return send(res, 200, fx.presets.list), true;
      if ((match = p.match(/^\/sim\/presets\/([^/]+)\/load$/)) && m === 'POST') {
        const s = fx.presets.scenarios[decodeURIComponent(match[1])];
        return s ? send(res, 200, setScenario(clone(s))) : send(res, 404, { detail: `no preset ${match[1]}` }), true;
      }
      if (p === '/sim/reset' && m === 'POST') return send(res, 200, setScenario(clone(fx.scenario.scenario))), true;
      if (p === '/sim/truth' && m === 'GET') return send(res, 200, truthNow()), true;
      send(res, 404, { detail: `no route ${m} ${p}` });
      return true;
    } catch (e) {
      send(res, 500, { detail: `mock error: ${e.message}` });
      return true;
    }
  }

  // ------------------------------------------------------------------ WebSocket
  const wss = new WebSocketServer({ noServer: true, perMessageDeflate: false });
  wss.on('connection', (ws) => {
    const client = { ws, subs: new Set() };
    state.clients.add(client);
    ws.on('message', (data, isBinary) => {
      if (isBinary) return;
      try {
        const msg = JSON.parse(String(data));
        if (msg.type === 'subscribe' && Array.isArray(msg.psd)) {
          client.subs = new Set(msg.psd.slice(0, 256).map((p) => `${p[0]}\u0000${p[1]}`));
        }
      } catch {
        /* ignore */
      }
    });
    ws.on('close', () => state.clients.delete(client));
    const s = liveStatus();
    ws.send(JSON.stringify({ type: 'status', ...s, marking: fx.meta.marking, simulation: fx.meta.simulation }));
  });

  function handleUpgrade(req, socket, head) {
    const url = new URL(req.url ?? '/', 'http://mock');
    if (url.pathname !== '/ws/ui') return false;
    if (!authorized(req, url)) {
      wss.handleUpgrade(req, socket, head, (ws) => ws.close(1008, 'bad token'));
      return true;
    }
    wss.handleUpgrade(req, socket, head, (ws) => wss.emit('connection', ws, req));
    return true;
  }

  // spectrum frames per window per second (2 like the monitors; MOCK_PSD_HZ raises it to soak-test the console)
  const psdRateHz = Number(options.psdRateHz ?? process.env.MOCK_PSD_HZ ?? 2) || 2;
  const timers = [
    setInterval(() => {
      if (!state.clients.size) return;
      const s = liveStatus();
      broadcast({ type: 'status', ...s, marking: fx.meta.marking, simulation: fx.meta.simulation });
    }, 1000),
    setInterval(() => {
      for (const c of state.clients) {
        if (c.ws.readyState !== 1 || c.ws.bufferedAmount > 4 * 1024 * 1024) continue; // never block on a slow client
        for (const key of c.subs) {
          const f = frameFor(key);
          if (f) c.ws.send(f, { binary: true });
        }
      }
    }, Math.max(20, Math.round(1000 / psdRateHz))),
    setInterval(() => state.clients.size && touchActive(), 5000),
  ];
  for (const t of timers) t.unref?.();

  return {
    handle,
    handleUpgrade,
    close() {
      for (const t of timers) clearInterval(t);
      for (const c of state.clients) c.ws.terminate();
      wss.close();
    },
  };
}
