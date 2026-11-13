// The mock's validator must say what the real server says: the same location and message
// as pydantic for the mutations mock/generate_fixtures.py ran through SystemConfig.
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { configWarnings, fillDerivedMonitors, validateConfig } from './validate.mjs';

const fx = (n) => JSON.parse(readFileSync(new URL(`./fixtures/${n}`, import.meta.url), 'utf8'));
const env = fx('config.json');
const schema = fx('config_schema.json');
const sample = fx('validate_sample.json');

describe('mock validator', () => {
  it('accepts the configuration it serves', () => {
    expect(validateConfig(structuredClone(env.config), schema)).toMatchObject({ ok: true, errors: [] });
  });

  it('reports field errors as pydantic does', () => {
    const cfg = structuredClone(env.config);
    cfg.sites[2].channels[0].antenna.diameter_m = -0.72;
    cfg.sites[0].lat_deg = 95.0;
    cfg.satellites[0].transponders[1].id = 'shared 2';
    expect(validateConfig(cfg, schema).errors.sort((a, b) => a.loc.localeCompare(b.loc))).toEqual([...sample.field.errors].sort((a, b) => a.loc.localeCompare(b.loc)));
  });

  it('reports cross-reference errors as SystemConfig does', () => {
    const cfg = structuredClone(env.config);
    cfg.sites[1].channels[0].pointing = { satellite_id: 'muos-atl' };
    expect(validateConfig(cfg, schema).errors).toEqual(sample.xref.errors);
    const cfg2 = structuredClone(env.config);
    cfg2.carriers[0].uplink_center_hz = 301.2e6;
    expect(validateConfig(cfg2, schema).errors).toEqual(sample.carrier.errors);
  });

  it('checks site time zones as Site.time_zone does', () => {
    const cfg = structuredClone(env.config);
    cfg.sites[0].time_zone = 'Pacific/Atlantis';
    cfg.sites[1].time_zone = 'Asia Tokyo';
    expect(validateConfig(cfg, schema).errors).toEqual(sample.time_zone.errors);
    const ok = structuredClone(env.config);
    ok.sites[0].time_zone = 'Pacific/Honolulu';
    expect(validateConfig(ok, schema)).toMatchObject({ ok: true, errors: [] });
  });

  it('warns about electrically small dishes like SystemConfig.warnings', () => {
    const real = env.warnings.filter((w) => w.msg.includes('reflector') || w.msg.includes('navg'));
    const mine = configWarnings(env.config).filter((w) => w.msg.includes('reflector') || w.msg.includes('navg'));
    expect(mine).toEqual(real);
  });

  it('derives the same monitor windows as the server', () => {
    const cfg = structuredClone(env.config);
    for (const s of cfg.sites) for (const c of s.channels) if (env.derived_monitors.includes(c.id)) c.monitors = [];
    const f = fillDerivedMonitors(cfg);
    expect(f.config.sites).toEqual(env.config.sites);
    expect(f.derived).toEqual(env.derived_monitors);
  });

  it('checks TLE lines and checksums', () => {
    const cfg = structuredClone(env.config);
    cfg.satellites[0].orbit = {
      type: 'tle',
      line1: '1 25544U 98067A   26317.50000000  .00016717  00000-0  10270-3 0  9993',
      line2: '2 25544  51.6400 208.9163 0006317  69.9862  25.2906 15.49560000 10000',
    };
    const r = validateConfig(cfg, schema);
    expect(r.ok).toBe(false);
    expect(r.errors.some((e) => e.loc.startsWith('satellites.0.orbit.line') && /checksum|69 characters/.test(e.msg))).toBe(true);
  });
});
