// Roll-up and node assessment against the status the server sends (UX-7, UX-10, SRV-1,
// the raised start-up floor, ADAPT-7).
import { describe, expect, it } from 'vitest';
import type { NodeStatus, SatelliteStatus, SiteClockEstimate } from '../api/types.gen';
import { nodeLevel, rollup, satelliteProblems, siteClockText, windowProblems } from './status';

const node = (patch: Partial<NodeStatus> = {}): NodeStatus => ({
  site_id: 'naha',
  node_id: 'n1',
  online: true,
  latency_ms: 10,
  clock: { source: 'gpsdo', locked: true, est_offset_ns: 2, holdover_s: 0 },
  driver: 'sim',
  software: 'x',
  connected_since: null,
  channels: [{ channel_id: 'naha-pac', ok: true, last_frame_age_s: 0.3, windows: [{ monitor_id: 'muos-pac:shared-1', blind: false, floor_raised_db: null }] }],
  ...patch,
});

const clock = (patch: Partial<SiteClockEstimate> = {}): SiteClockEstimate => ({
  site_id: 'naha',
  offset_us: -10.05,
  sigma_us: 0.12,
  n_calibrations: 39,
  compared_with: ['honolulu', 'tokyo'],
  caution: true,
  message: 'Naha: clock about 10 µs late by calibration',
  ...patch,
});

describe('header roll-up (UX-7)', () => {
  it('is Off, "No data", before any status: never a green all-clear', () => {
    const r = rollup({ status: null, live: false, events: [] });
    expect(r).toEqual({ level: 'off', word: 'No data', current: false });
  });
  it('keeps the last level but marks it not current while the link is down', () => {
    const r = rollup({ status: { nodes: [node()] }, live: false, events: [] });
    expect(r.level).toBe('normal');
    expect(r.current).toBe(false);
  });
  it('is the most urgent of nodes, satellites and active events while live', () => {
    expect(rollup({ status: { nodes: [node()] }, live: true, events: [] })).toEqual({ level: 'normal', word: 'Normal', current: true });
    expect(rollup({ status: { nodes: [node()] }, live: true, events: [{ state: 'located', verdict: 'uplink' }] }).level).toBe('critical');
    const down: SatelliteStatus = { sat_id: 'xs-150', ephemeris_ok: false, unavailable_since_ns: '1', message: 'satellite xs-150: ephemeris not available since ...' };
    expect(rollup({ status: { nodes: [node()], satellites: [down] }, live: true, events: [] }).level).toBe('serious');
    expect(rollup({ status: { nodes: [node()], site_clocks: [clock()] }, live: true, events: [] }).level).toBe('caution');
  });
});

describe('node assessment', () => {
  it('raises Caution for a site clock the calibrations put off (UX-10)', () => {
    const a = nodeLevel(node(), { siteClock: clock() });
    expect(a.level).toBe('caution');
    expect(a.reasons[0]).toBe('clock about −10.05 µs off by reference calibration');
    expect(nodeLevel(node(), { siteClock: clock({ caution: false, offset_us: 0.1 }) }).level).toBe('normal');
  });
  it('names a window blind to detection instead of a failed channel (SRV-1)', () => {
    const n = node({ channels: [{ channel_id: 'naha-pac', ok: false, last_frame_age_s: 0.3, windows: [{ monitor_id: 'muos-pac:shared-1', blind: true, blind_reason: 'out_of_order' }] }] });
    const a = nodeLevel(n);
    expect(a.level).toBe('caution');
    expect(a.reasons).toEqual(['detection blind on 1 window']);
  });
  it('flags a window whose noise floor a filling signal raised (K1)', () => {
    const n = node({ channels: [{ channel_id: 'naha-pac', ok: true, last_frame_age_s: 0.3, windows: [{ monitor_id: 'muos-pac:shared-1', floor_raised_db: 9.5 }] }] });
    expect(nodeLevel(n).reasons).toEqual(['noise floor raised on 1 window']);
    expect(windowProblems(n.channels[0]!)).toEqual({ blind: [], floorRaised: [{ id: 'muos-pac:shared-1', db: 9.5 }] });
  });
  it('still reports a channel that failed for another reason', () => {
    expect(nodeLevel(node({ channels: [{ channel_id: 'naha-pac', ok: false, last_frame_age_s: null }] })).reasons).toEqual(['1 channel failed']);
  });
});

describe('satellites and clock text (ADAPT-7, UX-10)', () => {
  it('lists only the satellites the server cannot place', () => {
    expect(satelliteProblems([{ sat_id: 'a', ephemeris_ok: true }, { sat_id: 'b', ephemeris_ok: false }]).map((s) => s.sat_id)).toEqual(['b']);
    expect(satelliteProblems(undefined)).toEqual([]);
  });
  it('formats offsets in ns, µs or ms', () => {
    expect(siteClockText({ offset_us: 0.118 })).toBe('118 ns');
    expect(siteClockText({ offset_us: -9.707 })).toBe('−9.71 µs');
    expect(siteClockText({ offset_us: 2500 })).toBe('2.50 ms');
  });
});
