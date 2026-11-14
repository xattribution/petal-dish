// The accuracy form starts from the loaded configuration (ADAPT-10) and offers
// "channel@sat" ends for channels watching several satellites (ADAPT-4).
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import type { AccuracyGrid, AccuracyPairSummary, SystemConfig } from '../api/types.gen';
import { accuracyDefaults, defaultReference, NEAREST_REFERENCE, pairCalibration, pairEnds, referenceChoice, referenceValues } from './plan';

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

// closing D2: the accuracy map says, per pair, what calibrates it and why cells are not
describe('reference default (closing D2)', () => {
  it('is "nearest" when the configuration has reference carriers, none otherwise', () => {
    expect(defaultReference(pacific)).toBe(NEAREST_REFERENCE);
    const bare = cfgOf([sat('a', [tp('t', 1e9, 1e6)]), sat('b', [tp('t', 1e9, 1e6)])], [site('s', [{ id: 'x', sats: ['a'] }, { id: 'y', sats: ['b'] }])]);
    expect(defaultReference(bare)).toBe('');
    expect(referenceValues(bare)).toEqual(['']);
  });
  it("keeps the operator's choice while the configuration offers it", () => {
    expect(referenceChoice(pacific, null)).toBe(NEAREST_REFERENCE);
    expect(referenceChoice(pacific, '')).toBe('');
    expect(referenceChoice(pacific, 'ref-guam')).toBe('ref-guam');
    expect(referenceChoice(pacific, 'ref-gone')).toBe(NEAREST_REFERENCE); // removed by a reload: back to the default
  });
});

describe('pair calibration in words (closing D2)', () => {
  const fixtures = JSON.parse(readFileSync(new URL('../../mock/fixtures/accuracy.json', import.meta.url), 'utf8')) as {
    variants: Array<{ reference_carrier_id: string | null; pairs: string[][]; kinds: string[]; grid: AccuracyGrid }>;
  };
  const base: AccuracyPairSummary = { a_channel: 'x', b_channel: 'y', a_sat_id: 'sat-a', b_sat_id: 'sat-b', calibrated: [], reference_carrier_id: null, reference_cells: {}, uncalibrated_cells: 100, reference_reason: null, reason: null, notes: [] };

  it('explains why the default request (nearest, a band over ref-guam) is uncalibrated', () => {
    const v = fixtures.variants.find((x) => x.reference_carrier_id === NEAREST_REFERENCE);
    expect(v).toBeDefined();
    const total = v!.grid.lat_deg.length * v!.grid.lon_deg.length;
    const views = (v!.grid.pairs ?? []).map((p) => pairCalibration(p, total));
    expect(views.map((x) => x.ends)).toEqual(['tokyo-pac − tokyo-io', 'tokyo-172e − tokyo-io']);
    expect(views[0]!.satellites).toBe('muos-pac − muos-io');
    for (const x of views) {
      expect(x.calibration).toBe('none (no eligible reference carrier)');
      expect(x.uncalibrated).toMatch(/^All 2501 cells are uncalibrated: no reference carrier clear of the target band is relayed by both transponders \(ref-guam overlaps the target band; /);
      expect(x.unusable).toBeNull();
    }
  });
  it('counts the cells a failing nearest carrier leaves uncalibrated', () => {
    const p = { ...base, calibrated: ['tdoa', 'fdoa'] as Array<'tdoa' | 'fdoa'>, reference_cells: { 'ref-1': 200, 'ref-weak': 55 }, uncalibrated_cells: 55, reference_reason: "reference 'ref-weak': predicted post-correlation SNR -48.0 dB below threshold" };
    const x = pairCalibration(p, 255);
    expect(x.calibration).toBe('nearest reference: ref-1 in 200 cells, ref-weak in 55 cells (TDOA, FDOA)');
    expect(x.uncalibrated).toBe("55 of 255 cells are uncalibrated: reference 'ref-weak': predicted post-correlation SNR -48.0 dB below threshold.");
  });
  it('names a calibrating carrier and stays quiet when every cell is calibrated', () => {
    const x = pairCalibration({ ...base, calibrated: ['fdoa'], reference_carrier_id: 'ref-1', reference_cells: { 'ref-1': 255 }, uncalibrated_cells: 0 }, 255);
    expect(x.calibration).toBe('ref-1 (FDOA)');
    expect(x.uncalibrated).toBeNull();
    const all = pairCalibration({ ...base, calibrated: ['tdoa'], reference_cells: { 'ref-1': 255 }, uncalibrated_cells: 0 }, 255);
    expect(all.calibration).toBe('nearest reference: ref-1 in every cell (TDOA)');
  });
  it('says when a named carrier cannot calibrate, when none was chosen, and when the pair gives nothing', () => {
    const named = pairCalibration({ ...base, reference_carrier_id: 'ref-1', reference_cells: { none: 255 }, uncalibrated_cells: 255, reference_reason: "reference 'ref-1' is not relayed by the pair's transponders" }, 255);
    expect(named.calibration).toBe('none (ref-1 cannot calibrate it)');
    expect(named.uncalibrated).toBe("All 255 cells are uncalibrated: reference 'ref-1' is not relayed by the pair's transponders.");
    const plain = pairCalibration({ ...base, uncalibrated_cells: 255 }, 255);
    expect(plain.calibration).toBe('none (no reference emitter chosen)');
    expect(plain.uncalibrated).toBeNull(); // the operator chose no reference: nothing to warn about
    const dead = pairCalibration({ ...base, a_sat_id: null, b_sat_id: null, reference_cells: { none: 255 }, uncalibrated_cells: 255, reason: "channel 'y' watches no satellite relaying 301000000 Hz" }, 255);
    expect(dead.satellites).toBeNull();
    expect(dead.unusable).toBe("This pair gives no measurement: channel 'y' watches no satellite relaying 301000000 Hz.");
    expect(dead.uncalibrated).toBeNull();
  });
});
