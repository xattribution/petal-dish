// How the console reads an event's new fields (UX-1, UX-3, UX-6, UX-9, K18).
import { describe, expect, it } from 'vitest';
import type { EvidenceRow, SolutionHistoryRow, SolutionOut, SolutionSummary, SystemConfig } from '../api/types.gen';
import { formatLat, formatLon } from '../format/geo';
import { candidates, isInconsistent, received, semiAxes, splitHistory, summaryCandidatesText, summaryReceivedText } from './event';

const ellipse = (a: number, b: number) => ({ semi_major_m: a, semi_minor_m: b, azimuth_deg: 10, confidence: 0.95 });
const zero = [
  [0, 0],
  [0, 0],
];
const sol = (patch: Partial<SolutionOut> = {}): SolutionOut => ({
  lat_deg: 20.31,
  lon_deg: 132.42,
  height_m: 0,
  ellipse: ellipse(4900, 600),
  cep50_m: 1500,
  cov_en_m2: zero,
  cov_noise_en_m2: zero,
  cov_consider_en_m2: zero,
  chi2: 10,
  dof: 8,
  reduced_chi2: 1.25,
  n_measurements: 10,
  dominant_error: 'noise',
  ambiguity_resolved_by: 'none',
  ambiguities: [],
  converged: true,
  ...patch,
} as SolutionOut);

describe('fit consistency (UX-1)', () => {
  it('treats only an explicit fit_consistent false as not a fix', () => {
    expect(isInconsistent(sol())).toBe(false);
    expect(isInconsistent(sol({ fit_consistent: true }))).toBe(false);
    expect(isInconsistent(sol({ fit_consistent: false, ellipse_scale: 5328 }))).toBe(true);
    expect(isInconsistent(null)).toBe(false);
  });
});

describe('unresolved ambiguity (UX-6)', () => {
  it('lists every candidate, best first, only while unresolved', () => {
    const mirror = sol({ lat_deg: -5.12, lon_deg: 133.2, chi2: 10.4, ellipse: ellipse(5200, 700) });
    const s = sol({ ambiguity_resolved_by: 'unresolved', ambiguities: [mirror] });
    const c = candidates(s);
    expect(c.map((x) => [x.lat, x.lon])).toEqual([
      [20.31, 132.42],
      [-5.12, 133.2],
    ]);
    expect(c[1]?.dChi2).toBeCloseTo(0.4, 9);
    expect(candidates(sol({ ambiguity_resolved_by: 'region', ambiguities: [mirror] }))).toEqual([]);
  });
  it('says "one of N positions" in the queue, never a single position', () => {
    const summary: SolutionSummary = { lat_deg: 20.31, lon_deg: 132.42, semi_major_m: 4900, semi_minor_m: 600, azimuth_deg: 0, cep50_m: 1, ambiguous: true, ambiguities: [{ lat_deg: -5.12, lon_deg: 133.2, semi_major_m: 1, semi_minor_m: 1, azimuth_deg: 0 }] };
    expect(summaryCandidatesText(summary)).toBe(`one of 2 positions: ${formatLat(20.31, 2)} ${formatLon(132.42, 2)} or ${formatLat(-5.12, 2)} ${formatLon(133.2, 2)}`);
    expect(summaryCandidatesText(summary)).toMatch(/^one of 2 positions: 20\.31.*N.*132\.42.*E or 5\.12.*S.*133\.20.*E$/);
    expect(summaryCandidatesText({ ...summary, ambiguous: false })).toBeNull();
  });
});

describe('one ellipse convention (UX-9)', () => {
  it('gives 95 % semi-axes in km', () => {
    expect(semiAxes(463.4, 243.5)).toBe('0.46 × 0.24 km');
  });
});

describe('local signal: what is received (UX-3)', () => {
  const cfg = { sites: [{ id: 'busan', name: 'Busan (city center)' }] } as unknown as SystemConfig;
  const row = (patch: Partial<EvidenceRow>): EvidenceRow => ({ channel_id: 'busan-pac', site_id: 'busan', sat_id: 'muos-pac', state: 'seen', snr_db: 12, cn0_dbhz: 50, predicted_margin_db: null, downlink_lo_hz: 260.0e6, downlink_hi_hz: 260.01e6, ...patch });
  it('quotes the downlink range, the sites and the channels that see it', () => {
    const ev = { evidence: [row({}), row({ channel_id: 'busan-io', sat_id: 'muos-io' }), row({ channel_id: 'busan-172e', sat_id: 'uhf-172e', state: 'not_capable', downlink_lo_hz: 267.4e6, downlink_hi_hz: 267.41e6 })], received_lo_hz: null, received_hi_hz: null, seen_site_ids: [] };
    expect(received(ev, cfg)).toEqual({
      range: '260.0000–260.0100 MHz',
      sites: ['Busan'],
      channels: [
        { channel: 'busan-pac', sat: 'muos-pac' },
        { channel: 'busan-io', sat: 'muos-io' },
      ],
    });
  });
  it('prefers the server range and sites, and gives the queue line', () => {
    expect(summaryReceivedText({ received_lo_hz: 260.0e6, received_hi_hz: 260.01e6, seen_site_ids: ['busan'] }, cfg)).toBe('260.0000–260.0100 MHz received at Busan');
    expect(summaryReceivedText({ received_lo_hz: null, received_hi_hz: null, seen_site_ids: [] }, cfg)).toBeNull();
  });
});

describe('ellipse history break (K18)', () => {
  const r = (t: string, setAside = false): SolutionHistoryRow => ({ t_ns: t, semi_major_m: 1000, semi_minor_m: 100, n_measurements: 4, set_aside: setAside });
  it('splits set-aside solves from the current transmission and names the break', () => {
    const s = splitHistory([r('1', true), r('2', true), r('3'), r('4')]);
    expect(s.setAside.map((x) => x.t_ns)).toEqual(['1', '2']);
    expect(s.current.map((x) => x.t_ns)).toEqual(['3', '4']);
    expect(s.breakNs).toBe('3');
    expect(splitHistory([r('1'), r('2')]).breakNs).toBeNull();
  });
});
