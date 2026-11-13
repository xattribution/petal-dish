// Configuration and scenario validation for the mock server, phrased the way the real
// server phrases it (pydantic messages, SystemConfig cross-reference checks, the
// warnings of SystemConfig.warnings). It covers the JSON Schema constraints generically
// and the model validators an operator is likely to trip over in the console; the real
// server remains the authority.

const C = 299792458;

function fmt(v) {
  return typeof v === 'number' ? String(v) : JSON.stringify(v);
}

function resolve(schema, node) {
  let n = node;
  while (n && n.$ref) n = schema.$defs[n.$ref.split('/').pop()];
  return n ?? {};
}

const isObj = (v) => typeof v === 'object' && v !== null && !Array.isArray(v);

/** Validate `value` against a JSON Schema node; push {loc, msg} problems. */
function check(schema, nodeIn, value, path, out) {
  const node = resolve(schema, nodeIn);
  if (node.anyOf) {
    if (value === null && node.anyOf.some((b) => resolve(schema, b).type === 'null')) return;
    const branches = node.anyOf.filter((b) => resolve(schema, b).type !== 'null');
    let best = null;
    for (const b of branches) {
      const tmp = [];
      check(schema, b, value, path, tmp);
      if (!tmp.length) return;
      if (!best || tmp.length < best.length) best = tmp;
    }
    if (best) out.push(...best);
    return;
  }
  if (node.oneOf) {
    const disc = node.discriminator?.propertyName;
    if (disc) {
      if (!isObj(value)) return out.push({ loc: path, msg: 'Input should be a valid dictionary or object to extract fields from' });
      const tag = value[disc];
      const ref = node.discriminator.mapping[tag];
      if (tag === undefined) return out.push({ loc: path, msg: `Unable to extract tag using discriminator '${disc}'` });
      if (!ref) {
        const tags = Object.keys(node.discriminator.mapping).map((t) => `'${t}'`).join(', ');
        return out.push({ loc: path, msg: `Input tag '${tag}' found using '${disc}' does not match any of the expected tags: ${tags}` });
      }
      return check(schema, { $ref: ref }, value, path, out);
    }
    // callable discriminators (pointing): a string is "omni", an object with satellite_id tracks
    const branches = node.oneOf.map((b) => resolve(schema, b));
    let pick;
    if (typeof value === 'string') pick = branches.find((b) => b.const !== undefined);
    else if (isObj(value)) pick = branches.find((b) => b.properties && ('satellite_id' in value ? 'satellite_id' in b.properties && !('satellite_ids' in b.properties) : !('satellite_id' in (b.properties ?? {}))));
    if (!pick) {
      const forms = branches.some((b) => b.const === 'omni') ? 'must be "omni", {satellite_id} or {az_deg, el_deg}' : 'must be {satellite_id} or {az_deg, el_deg, satellite_ids}';
      return out.push({ loc: path, msg: forms });
    }
    if (pick.const !== undefined) {
      if (value !== pick.const) out.push({ loc: path, msg: `Input should be '${pick.const}'` });
      return;
    }
    return check(schema, pick, value, path, out);
  }
  if (node.const !== undefined && value !== node.const) return out.push({ loc: path, msg: `Input should be ${fmt(node.const)}` });
  if (node.enum && !node.enum.includes(value)) {
    const opts = node.enum.map(fmt);
    const text = opts.length > 1 ? `${opts.slice(0, -1).join(', ')} or ${opts.at(-1)}` : opts[0];
    return out.push({ loc: path, msg: `Input should be ${text}` });
  }
  switch (node.type) {
    case 'number':
    case 'integer': {
      if (typeof value !== 'number' || !Number.isFinite(value)) {
        return out.push({ loc: path, msg: node.type === 'integer' ? 'Input should be a valid integer' : 'Input should be a valid number' });
      }
      if (node.type === 'integer' && !Number.isInteger(value)) return out.push({ loc: path, msg: 'Input should be a valid integer, got a number with a fractional part' });
      if (node.exclusiveMinimum !== undefined && !(value > node.exclusiveMinimum)) out.push({ loc: path, msg: `Input should be greater than ${node.exclusiveMinimum}` });
      if (node.minimum !== undefined && !(value >= node.minimum)) out.push({ loc: path, msg: `Input should be greater than or equal to ${node.minimum}` });
      if (node.exclusiveMaximum !== undefined && !(value < node.exclusiveMaximum)) out.push({ loc: path, msg: `Input should be less than ${node.exclusiveMaximum}` });
      if (node.maximum !== undefined && !(value <= node.maximum)) out.push({ loc: path, msg: `Input should be less than or equal to ${node.maximum}` });
      return;
    }
    case 'string':
      if (typeof value !== 'string') return out.push({ loc: path, msg: 'Input should be a valid string' });
      if (node.minLength && value.length < node.minLength) out.push({ loc: path, msg: `String should have at least ${node.minLength} character${node.minLength === 1 ? '' : 's'}` });
      if (node.pattern && !new RegExp(node.pattern).test(value)) out.push({ loc: path, msg: `String should match pattern '${node.pattern}'` });
      return;
    case 'boolean':
      if (typeof value !== 'boolean') out.push({ loc: path, msg: 'Input should be a valid boolean' });
      return;
    case 'null':
      if (value !== null) out.push({ loc: path, msg: 'Input should be None' });
      return;
    case 'array':
      if (!Array.isArray(value)) return out.push({ loc: path, msg: 'Input should be a valid list' });
      if (node.minItems !== undefined && value.length < node.minItems) out.push({ loc: path, msg: `List should have at least ${node.minItems} item${node.minItems === 1 ? '' : 's'} after validation, not ${value.length}` });
      if (node.maxItems !== undefined && value.length > node.maxItems) out.push({ loc: path, msg: `List should have at most ${node.maxItems} item${node.maxItems === 1 ? '' : 's'} after validation, not ${value.length}` });
      value.forEach((v, i) => check(schema, node.prefixItems?.[i] ?? node.items ?? {}, v, [...path, i], out));
      return;
    case 'object': {
      if (!isObj(value)) return out.push({ loc: path, msg: 'Input should be a valid dictionary' });
      for (const req of node.required ?? []) if (!(req in value)) out.push({ loc: [...path, req], msg: 'Field required' });
      for (const [k, v] of Object.entries(value)) {
        const prop = node.properties?.[k];
        if (prop) check(schema, prop, v, [...path, k], out);
        else if (node.additionalProperties === false) out.push({ loc: [...path, k], msg: 'Extra inputs are not permitted' });
        else if (isObj(node.additionalProperties)) check(schema, node.additionalProperties, v, [...path, k], out);
      }
      return;
    }
    default:
      return;
  }
}

// --------------------------------------------------------------------------- model validators

function tleChecksum(line) {
  let s = 0;
  for (const ch of line.slice(0, 68)) {
    if (ch >= '0' && ch <= '9') s += Number(ch);
    else if (ch === '-') s += 1;
  }
  return s % 10;
}

function modelChecks(cfg, out) {
  const ve = (loc, msg) => out.push({ loc, msg: `Value error, ${msg}` });
  (cfg.satellites ?? []).forEach((sat, k) => {
    const o = sat?.orbit;
    if (o?.type === 'tle') {
      for (const [name, num] of [['line1', '1'], ['line2', '2']]) {
        const line = typeof o[name] === 'string' ? o[name].trimEnd() : null;
        if (line === null) continue;
        if (line.length !== 69) ve(['satellites', k, 'orbit', name], `a TLE line is 69 characters long, got ${line.length}`);
        else if (line[0] !== num) ve(['satellites', k, 'orbit', name], `${name} must start with '${num}'`);
        else if (!/\d/.test(line[68]) || Number(line[68]) !== tleChecksum(line)) ve(['satellites', k, 'orbit', name], `TLE checksum mismatch: expected ${tleChecksum(line)}, found '${line[68]}'`);
      }
    }
    (sat?.transponders ?? []).forEach((tp, t) => {
      if (typeof tp?.uplink_center_hz !== 'number' || typeof tp?.bandwidth_hz !== 'number') return;
      if (tp.uplink_center_hz - tp.bandwidth_hz / 2 <= 0) ve(['satellites', k, 'transponders', t], 'bandwidth_hz: the uplink passband must lie above 0 Hz');
      else if (typeof tp.translation_hz === 'number' && tp.uplink_center_hz + tp.translation_hz - tp.bandwidth_hz / 2 <= 0) {
        ve(['satellites', k, 'transponders', t], 'translation_hz: the downlink passband (uplink + translation) must lie above 0 Hz');
      }
    });
  });
  (cfg.sites ?? []).forEach((site, s) => {
    const tz = site?.time_zone;
    if (typeof tz === 'string') {
      const known = /^[A-Za-z][A-Za-z0-9_+-]{0,29}(\/[A-Za-z0-9_+-]{1,30}){0,2}$/.test(tz) && !tz.includes('..');
      if (!known) ve(['sites', s, 'time_zone'], `must be an IANA time zone name such as Asia/Tokyo, got '${tz}'`);
      else {
        try {
          new Intl.DateTimeFormat('en-US', { timeZone: tz });
        } catch {
          ve(['sites', s, 'time_zone'], `unknown IANA time zone '${tz}' (for example Asia/Tokyo or Pacific/Honolulu)`);
        }
      }
    }
    (site?.channels ?? []).forEach((ch, c) => {
      const rate = ch?.max_sample_rate_hz ?? 2e6;
      (ch?.monitors ?? []).forEach((mon, mi) => {
        if (typeof mon?.span_hz !== 'number') return;
        const nfft = mon.nfft ?? 1024;
        const navg = mon.navg ?? 8;
        const fr = mon.frame_rate_hz ?? 2;
        if (nfft * navg * fr > mon.span_hz * (1 + 1e-12)) {
          ve(['sites', s, 'channels', c, 'monitors', mi], `monitor '${mon.id}': a frame of nfft * navg = ${nfft * navg} samples at span_hz = ${mon.span_hz} Hz lasts ${(nfft * navg) / mon.span_hz} s, longer than the frame period 1 / frame_rate_hz = ${1 / fr} s`);
        }
        if (mon.span_hz > rate * (1 + 1e-12)) ve(['sites', s, 'channels', c], `monitor '${mon.id}': span_hz = ${mon.span_hz} Hz exceeds the channel's max_sample_rate_hz = ${rate} Hz`);
      });
      const p = ch?.pointing;
      if (p && Array.isArray(p.satellite_ids) && new Set(p.satellite_ids).size !== p.satellite_ids.length) {
        ve(['sites', s, 'channels', c, 'pointing'], 'satellite_ids: a satellite is listed more than once');
      }
    });
  });
  (cfg.carriers ?? []).forEach((car, k) => {
    if (car?.use_as_reference && (!car.location || !car.emitter)) {
      const missing = [!car.location && 'location', !car.emitter && 'emitter'].filter(Boolean).join(' and ');
      ve(['carriers', k], `use_as_reference: a reference carrier needs ${missing}`);
    }
    if (car?.emitter && car.emitter.waveform !== 'cw' && !(car.emitter.bandwidth_hz > 0)) {
      ve(['carriers', k, 'emitter'], `bandwidth_hz: must be positive for a '${car.emitter.waveform ?? 'noise'}' waveform`);
    }
  });
  const det = cfg.processing?.detection;
  if (det && (det.confirm_m ?? 3) > (det.confirm_n ?? 5)) ve(['processing', 'detection'], `confirm_m: ${det.confirm_m ?? 3} detections cannot be seen in confirm_n = ${det.confirm_n ?? 5} frames`);
  const reg = cfg.processing?.region;
  if (reg && typeof reg.lat_min === 'number' && typeof reg.lat_max === 'number' && !(reg.lat_min < reg.lat_max)) ve(['processing', 'region'], 'lat_max: must be greater than lat_min');
}

function crossChecks(cfg, out) {
  const unique = (entries, what) => {
    const first = new Map();
    for (const [loc, id] of entries) {
      if (typeof id !== 'string') continue;
      if (first.has(id)) out.push({ loc: [...loc, 'id'], msg: `${what} id '${id}' is already used at ${first.get(id).join('.')}` });
      else first.set(id, loc);
    }
  };
  const sats = new Map((cfg.satellites ?? []).map((s) => [s?.id, s]));
  unique((cfg.satellites ?? []).map((s, k) => [['satellites', k], s?.id]), 'satellite');
  unique((cfg.sites ?? []).map((s, k) => [['sites', k], s?.id]), 'site');
  unique((cfg.carriers ?? []).map((c, k) => [['carriers', k], c?.id]), 'carrier');
  unique((cfg.sites ?? []).flatMap((s, si) => (s?.channels ?? []).map((c, ci) => [['sites', si, 'channels', ci], c?.id])), 'channel');
  (cfg.satellites ?? []).forEach((s, k) => unique((s?.transponders ?? []).map((t, ti) => [['satellites', k, 'transponders', ti], t?.id]), `transponder (of satellite '${s?.id}')`));
  (cfg.sites ?? []).forEach((site, s) =>
    (site?.channels ?? []).forEach((ch, c) => {
      const p = ch?.pointing;
      const loc = ['sites', s, 'channels', c, 'pointing'];
      if (p && typeof p.satellite_id === 'string' && !sats.has(p.satellite_id)) out.push({ loc: [...loc, 'satellite_id'], msg: `unknown satellite id '${p.satellite_id}'` });
      if (p && Array.isArray(p.satellite_ids)) p.satellite_ids.forEach((id, i) => !sats.has(id) && out.push({ loc: [...loc, 'satellite_ids', i], msg: `unknown satellite id '${id}'` }));
    }),
  );
  (cfg.carriers ?? []).forEach((car, k) => {
    const sat = sats.get(car?.satellite_id);
    if (!sat) return out.push({ loc: ['carriers', k, 'satellite_id'], msg: `unknown satellite id '${car?.satellite_id}'` });
    const tp = (sat.transponders ?? []).find((t) => t.id === car.transponder_id);
    if (!tp) return out.push({ loc: ['carriers', k, 'transponder_id'], msg: `unknown transponder id '${car.transponder_id}' on satellite '${sat.id}'` });
    const lo = tp.uplink_center_hz - tp.bandwidth_hz / 2;
    const hi = tp.uplink_center_hz + tp.bandwidth_hz / 2;
    if (!(car.uplink_center_hz >= lo && car.uplink_center_hz <= hi)) {
      out.push({ loc: ['carriers', k, 'uplink_center_hz'], msg: `${Math.round(car.uplink_center_hz)} Hz is outside the uplink passband of transponder '${tp.id}' (${Math.round(lo)} to ${Math.round(hi)} Hz)` });
    }
  });
}

/** Non-blocking findings in the shape of SystemConfig.warnings(). */
export function configWarnings(cfg) {
  const notes = [];
  const sats = new Map((cfg.satellites ?? []).map((s) => [s.id, s]));
  (cfg.carriers ?? []).forEach((car, k) => {
    const tp = sats.get(car.satellite_id)?.transponders?.find((t) => t.id === car.transponder_id);
    if (!tp) return;
    const [lo, hi] = [tp.uplink_center_hz - tp.bandwidth_hz / 2, tp.uplink_center_hz + tp.bandwidth_hz / 2];
    const [clo, chi] = [car.uplink_center_hz - car.bandwidth_hz / 2, car.uplink_center_hz + car.bandwidth_hz / 2];
    if (clo < lo - 1e-6 || chi > hi + 1e-6) {
      notes.push({ loc: `carriers.${k}.bandwidth_hz`, msg: `carrier '${car.id}' (${Math.round(clo)} to ${Math.round(chi)} Hz) extends beyond the uplink passband of transponder '${tp.id}' (${Math.round(lo)} to ${Math.round(hi)} Hz)` });
    }
  });
  (cfg.carriers ?? []).forEach((car, k) => {
    const a = car.emitter?.antenna;
    if (a?.type === 'parabolic' && a.diameter_m < (3 * C) / car.uplink_center_hz) {
      notes.push({ loc: `carriers.${k}.emitter.antenna`, msg: `carrier '${car.id}': a ${a.diameter_m} m dish is under 3 wavelengths at ${(car.uplink_center_hz / 1e6).toFixed(3)} MHz; it behaves as a reflector with a broad beam, not as a focusing dish` });
    }
  });
  (cfg.sites ?? []).forEach((site, s) =>
    (site.channels ?? []).forEach((ch, c) => {
      const base = `sites.${s}.channels.${c}`;
      for (const m of ch.monitors ?? []) {
        if ((m.navg ?? 8) < 8) {
          notes.push({ loc: `${base}.monitors`, msg: `channel '${ch.id}': monitor '${m.id}' averages only navg = ${m.navg} spectra per frame; the detection threshold and the C/N0 estimate assume at least 8 (more false alarms, C/N0 reads high)` });
        }
      }
      if (ch.antenna?.type !== 'parabolic') return;
      const watched = ch.pointing?.satellite_id ? [ch.pointing.satellite_id] : ch.pointing?.satellite_ids ?? [];
      // the lowest frequency the channel works at: watched downlinks and its monitor centres
      const working = [
        ...watched.flatMap((id) => (sats.get(id)?.transponders ?? []).map((t) => t.uplink_center_hz + t.translation_hz)),
        ...(ch.monitors ?? []).map((m) => m.center_hz),
      ];
      if (!working.length) return;
      const lowest = Math.min(...working);
      if (ch.antenna.diameter_m < (3 * C) / lowest) {
        notes.push({ loc: `${base}.antenna`, msg: `channel '${ch.id}': a ${ch.antenna.diameter_m} m dish is under 3 wavelengths at ${(lowest / 1e6).toFixed(3)} MHz; it behaves as a reflector with a broad beam, not as a focusing dish` });
      }
    }),
  );
  return notes;
}

/** POST /config/validate result: {ok, errors, warnings}. */
export function validateConfig(cfg, schema) {
  const problems = [];
  check(schema, schema, cfg, [], problems);
  if (!problems.length) modelChecks(cfg, problems);
  if (!problems.length) crossChecks(cfg, problems);
  const errors = problems.map((p) => ({ loc: p.loc.join('.') || '(root)', msg: p.msg }));
  return { ok: errors.length === 0, errors, warnings: errors.length ? [] : configWarnings(cfg) };
}

/** Problems of a scenario against the configuration, [{loc, msg}] with dotted locations. */
export function validateScenario(s, cfg) {
  const out = [];
  if (typeof s.name !== 'string' || !s.name) out.push({ loc: 'name', msg: 'Field required' });
  const sats = new Set((cfg.satellites ?? []).map((x) => x.id));
  const sites = new Set((cfg.sites ?? []).map((x) => x.id));
  const carriers = new Set((cfg.carriers ?? []).map((x) => x.id));
  const seen = new Set();
  (s.emitters ?? []).forEach((e, i) => {
    const at = `emitters.${i}`;
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(e.id ?? '')) out.push({ loc: `${at}.id`, msg: "String should match pattern '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'" });
    if (seen.has(e.id)) out.push({ loc: `${at}.id`, msg: `Value error, emitters[${i}].id: duplicate emitter id '${e.id}'` });
    seen.add(e.id);
    if (e.kind === 'carrier' && !e.carrier_id) out.push({ loc: at, msg: "Value error, carrier_id: required when kind is 'carrier'" });
    if (e.carrier_id && !carriers.has(e.carrier_id)) out.push({ loc: `${at}.carrier_id`, msg: `unknown carrier '${e.carrier_id}'` });
    if (!e.carrier_id) {
      for (const f of ['lat_deg', 'lon_deg', 'freq_hz', 'eirp_dbw', 'waveform']) {
        if (e[f] === undefined || e[f] === null) {
          out.push({ loc: at, msg: `Value error, ${f}: required for an emitter without carrier_id` });
          break;
        }
      }
    }
    if (typeof e.lat_deg === 'number' && (e.lat_deg < -90 || e.lat_deg > 90)) out.push({ loc: `${at}.lat_deg`, msg: 'Input should be less than or equal to 90' });
    if (typeof e.lon_deg === 'number' && (e.lon_deg < -180 || e.lon_deg > 180)) out.push({ loc: `${at}.lon_deg`, msg: 'Input should be less than or equal to 180' });
    if (typeof e.freq_hz === 'number' && !(e.freq_hz > 0)) out.push({ loc: `${at}.freq_hz`, msg: 'Input should be greater than 0' });
    const w = e.waveform;
    if (w) {
      if ((w.type === 'noise' || w.type === 'chirp') && !(w.bandwidth_hz > 0)) out.push({ loc: `${at}.waveform`, msg: `Value error, bandwidth_hz: must be positive for a '${w.type}' waveform` });
      if (w.type === 'psk' && !(w.symbol_rate_hz > 0) && !(w.bandwidth_hz > 0)) out.push({ loc: `${at}.waveform`, msg: 'Value error, symbol_rate_hz: a psk waveform needs symbol_rate_hz or bandwidth_hz' });
      if (w.duty !== undefined && w.duty < 1 && !(w.period_s > 0)) out.push({ loc: `${at}.waveform`, msg: 'Value error, period_s: must be positive when duty < 1' });
    }
    const target = e.pointing && typeof e.pointing === 'object' ? e.pointing.satellite_id : undefined;
    if (target !== undefined && !sats.has(target)) out.push({ loc: `${at}.pointing.satellite_id`, msg: `unknown satellite '${target}'` });
  });
  const errs = s.errors ?? {};
  for (const [field, known, what] of [['ephemeris', sats, 'satellite'], ['sat_osc_frac', sats, 'satellite'], ['site_clock_offset_ns', sites, 'site'], ['site_osc_frac', sites, 'site']]) {
    for (const key of Object.keys(errs[field] ?? {})) if (!known.has(key)) out.push({ loc: `errors.${field}`, msg: `unknown ${what} '${key}'` });
  }
  return out;
}

// --------------------------------------------------------------------------- derived monitors

function fitMonitorFrame(spanHz, frameRateHz = 2, nfft0 = 1024, navg0 = 8) {
  const budget = (spanHz * (1 + 1e-12)) / frameRateHz;
  let nfft = nfft0;
  const navg = Math.max(1, Math.min(navg0, Math.floor(budget / nfft)));
  while (nfft * navg > budget && nfft / 2 >= 64) nfft /= 2;
  if (nfft * navg > budget) return null;
  return [nfft, navg];
}

/**
 * Fill the monitors of channels that configure none, the rule of config/models.py
 * derive_monitors; returns the configuration and the ids of the channels so filled.
 */
export function fillDerivedMonitors(cfg) {
  const sats = new Map((cfg.satellites ?? []).map((s) => [s.id, s]));
  const derived = [];
  const sites = (cfg.sites ?? []).map((site) => ({
    ...site,
    channels: (site.channels ?? []).map((ch) => {
      if (ch.monitors && ch.monitors.length) return ch;
      const watched = ch.pointing?.satellite_id ? [ch.pointing.satellite_id] : ch.pointing?.satellite_ids ?? [];
      const seen = new Set();
      const monitors = [];
      for (const id of watched) {
        for (const tp of sats.get(id)?.transponders ?? []) {
          const span = Math.min(1.25 * tp.bandwidth_hz, ch.max_sample_rate_hz ?? 2e6);
          const center = tp.uplink_center_hz + tp.translation_hz;
          const key = `${center}|${span}`;
          if (seen.has(key)) continue;
          seen.add(key);
          const fit = fitMonitorFrame(span);
          if (fit) monitors.push({ id: `${id}:${tp.id}`, center_hz: center, span_hz: span, nfft: fit[0], navg: fit[1], frame_rate_hz: 2 });
        }
      }
      derived.push(ch.id);
      return { ...ch, monitors };
    }),
  }));
  return { config: { ...cfg, sites }, derived };
}
