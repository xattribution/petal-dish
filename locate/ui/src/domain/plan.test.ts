// The accuracy form starts from the loaded configuration (ADAPT-10) and offers
// "channel@sat" ends for channels watching several satellites (ADAPT-4).
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import type { SystemConfig } from '../api/types.gen';
import { accuracyDefaults, pairEnds } from './plan';

const pacific = (JSON.parse(readFileSync(new URL('../../mock/fixtures/config.json', import.meta.url), 'utf8')) as { config: SystemConfig }).config;

const tp = (id: string, f: number, bw: number) => ({ id, uplink_center_hz: f, bandwidth_hz: bw, translation_hz: -1e9, g_over_t_dbk: 0, sfd_dbw_m2: -90, eirp_sat_dbw: 50 });
const ant = { type: 'parabolic' as const, diameter_m: 2.4, efficiency: 0.6 };
const sat = (id: string, tps: ReturnType<typeof tp>[]) => ({ id, name: id, orbit: { type: 'geo' as const, lon_deg: 140 }, transponders: tps });
const site = (id: string, channels: Array<{ id: string; sats: string[] }>) => ({
  id,
  name: id,
  lat_deg: 0,
  lon_deg: 0,
  channels: channels.map((c) => ({ id: c.id, antenna: ant, pointing: c.sats.length > 1 ? { type: 'fixed', az_deg: 0, el_deg: 90, satellite_ids: c.sats } : { satellite_id: c.sats[0] } })),
});
const cfgOf = (sats: unknown[], sites: unknown[]) => ({ name: 'x', satellites: sats, sites, carriers: [] }) as unknown as SystemConfig;

describe('accuracy form defaults (ADAPT-10)', () => {
  it('pairs the Pacific example at the site watching all three satellites, in the band they share', () => {
    const d = accuracyDefaults(pacific);
    expect(d.pairs).toEqual([
      ['tokyo-pac', 'tokyo-io'],
      ['tokyo-172e', 'tokyo-io'],
    ]);
    expect(d.feHz).toBe(301_000_000);
    expect(d.bwHz).toBe(8000);
  });
  it('works for a Ku-band configuration (wide transponders, bandwidth capped)', () => {
    const ku = cfgOf(
      ['ku-a', 'ku-b', 'ku-c'].map((s) => sat(s, [tp('k1', 14.25e9, 36e6)])),
      [site('tokyo', [{ id: 'tokyo-ku-a', sats: ['ku-a'] }, { id: 'tokyo-ku-b', sats: ['ku-b'] }, { id: 'tokyo-ku-c', sats: ['ku-c'] }])],
    );
    expect(accuracyDefaults(ku)).toEqual({
      pairs: [
        ['tokyo-ku-a', 'tokyo-ku-c'],
        ['tokyo-ku-b', 'tokyo-ku-c'],
      ],
      feHz: 14.25e9,
      bwHz: 5e6,
    });
  });
  it('uses the overlap of partly overlapping transponders and falls back to a pair across sites', () => {
    const cfg = cfgOf([sat('a', [tp('t', 435.1e6, 100e3)]), sat('b', [tp('t', 435.15e6, 100e3)])], [site('s1', [{ id: 's1-a', sats: ['a'] }]), site('s2', [{ id: 's2-b', sats: ['b'] }])]);
    const d = accuracyDefaults(cfg);
    expect(d.pairs).toEqual([['s1-a', 's2-b']]);
    expect(d.feHz).toBe(435.125e6);
    expect(d.bwHz).toBe(16000);
  });
  it('has no pair when one channel watches both satellites (a channel cannot pair with itself)', () => {
    const leo = cfgOf([sat('leo-a', [tp('u1', 435.1e6, 100e3)]), sat('leo-b', [tp('u1', 435.1e6, 100e3)])], [site('naha', [{ id: 'naha-leo', sats: ['leo-a', 'leo-b'] }])]);
    expect(accuracyDefaults(leo)).toEqual({ pairs: [], feHz: null, bwHz: 8000 });
  });
});

describe('pair ends (ADAPT-4)', () => {
  it('names the satellite for a channel watching several', () => {
    const cfg = cfgOf([sat('a', [tp('t', 1e9, 1e6)]), sat('b', [tp('t', 1e9, 1e6)])], [site('s', [{ id: 'fix', sats: ['a', 'b'] }, { id: 'one', sats: ['a'] }])]);
    expect(pairEnds(cfg).map((e) => e.value)).toEqual(['fix@a', 'fix@b', 'one']);
    // satellite a through "one" (it watches only a), b through fix@b
    expect(accuracyDefaults(cfg).pairs).toEqual([['fix@b', 'one']]);
  });
  it('pairs a multi-satellite channel by its explicit end', () => {
    const cfg = cfgOf([sat('a', [tp('t', 1e9, 1e6)]), sat('b', [tp('t', 1e9, 1e6)])], [site('s', [{ id: 'one', sats: ['a'] }, { id: 'fix', sats: ['a', 'b'] }])]);
    // ends at the site, one per satellite: one (a), fix@b (b); the pair shares the last
    expect(accuracyDefaults(cfg).pairs).toEqual([['one', 'fix@b']]);
  });
});
