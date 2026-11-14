import { describe, expect, it } from 'vitest';
import type { ReceiveChannel, Satellite } from '../api/types.gen';
import { deriveMonitors, electricallySmall, fitMonitorFrame } from './config';
import { compareEvents, eventLevel, nodeLevel, worst } from './status';

const sat: Satellite = {
  id: 'muos-pac', name: 'x', orbit: { type: 'geo', lon_deg: -177 },
  transponders: [
    { id: 'shared-1', uplink_center_hz: 301.0e6, bandwidth_hz: 25e3, translation_hz: -41e6, g_over_t_dbk: -14, sfd_dbw_m2: -136, eirp_sat_dbw: 26 },
    { id: 'solo-pac', uplink_center_hz: 301.5e6, bandwidth_hz: 25e3, translation_hz: -41e6, g_over_t_dbk: -14, sfd_dbw_m2: -136, eirp_sat_dbw: 26 },
  ],
};
const ch = (diameter: number): ReceiveChannel => ({ id: 'c', antenna: { type: 'parabolic', diameter_m: diameter, efficiency: 0.55 }, pointing: { satellite_id: 'muos-pac' } });

describe('monitor derivation (same rule as config/models.py)', () => {
  it('fits 1024 x 8 into a 31.25 kHz window at 2 frames per second', () => {
    expect(fitMonitorFrame(31250)).toEqual([1024, 8]);
    expect(fitMonitorFrame(4000)).toEqual([1024, 1]);
    expect(fitMonitorFrame(1000)).toEqual([256, 1]);
    expect(() => fitMonitorFrame(100)).toThrow();
  });
  it('derives one window per transponder named <sat>:<transponder>', () => {
    const m = deriveMonitors(ch(1.2), [sat]);
    expect(m.map((x) => x.id)).toEqual(['muos-pac:shared-1', 'muos-pac:solo-pac']);
    expect(m[0]).toMatchObject({ center_hz: 260.0e6, span_hz: 31250, nfft: 1024, navg: 8, frame_rate_hz: 2 });
  });
});

describe('electrically small parabolic antennas', () => {
  it('flags a 0.72 m and a 1.2 m dish at 260 MHz (wavelength 1.15 m)', () => {
    const s = electricallySmall(ch(0.72), [sat]);
    expect(s).not.toBeNull();
    expect(s!.wavelengthM).toBeCloseTo(1.153, 3);
    expect(s!.diametersInWavelengths).toBeCloseTo(0.624, 3);
    expect(s!.hpbwDeg).toBe(112.1 > 120 ? 120 : s!.hpbwDeg);
    expect(electricallySmall(ch(1.2), [sat])).not.toBeNull();
  });
  it('does not flag a dish of 3 wavelengths or more', () => {
    expect(electricallySmall(ch(3.5), [sat])).toBeNull();
  });
});

describe('status ladder', () => {
  it('maps events and orders the queue most urgent first', () => {
    expect(eventLevel({ state: 'located', verdict: 'uplink' })).toBe('critical');
    expect(eventLevel({ state: 'detected', verdict: 'local' })).toBe('serious');
    expect(eventLevel({ state: 'detected', verdict: 'undetermined' })).toBe('caution');
    expect(eventLevel({ state: 'lost', verdict: 'uplink' })).toBe('standby');
    expect(eventLevel({ state: 'closed', verdict: 'uplink' })).toBe('off');
    const mk = (id: string, state: string, verdict: string, t: string) => ({ id, state, verdict, last_seen_ns: t, first_seen_ns: t, uplink_lo_hz: 1, uplink_hi_hz: 2, satellite_ids: [], label: null, headline: '', solution: null }) as Parameters<typeof compareEvents>[0];
    const list = [mk('a', 'lost', 'uplink', '9'), mk('b', 'detected', 'undetermined', '5'), mk('c', 'located', 'uplink', '3'), mk('d', 'located', 'uplink', '4')];
    expect(list.sort(compareEvents).map((e) => e.id)).toEqual(['d', 'c', 'b', 'a']);
    expect(worst(['normal', 'caution', 'off'])).toBe('caution');
  });
  it('rates nodes: offline serious, unlocked clock caution', () => {
    const base = { site_id: 's', node_id: 'n', online: true, latency_ms: 1, driver: 'sim', software: 'x', connected_since: null, channels: [{ channel_id: 'c', ok: true, last_frame_age_s: 0.3 }] };
    expect(nodeLevel({ ...base, clock: { source: 'gpsdo', locked: true, est_offset_ns: 1, holdover_s: 0 } }).level).toBe('normal');
    expect(nodeLevel({ ...base, clock: { source: 'ntp', locked: false, est_offset_ns: 1, holdover_s: 40 } }).level).toBe('caution');
    expect(nodeLevel({ ...base, online: false, clock: null }).level).toBe('serious');
    expect(nodeLevel({ ...base, clock: { source: 'gpsdo', locked: true, est_offset_ns: 1, holdover_s: 0 }, channels: [{ channel_id: 'c', ok: true, last_frame_age_s: 9 }] }).reasons[0]).toContain('stale');
  });
});
