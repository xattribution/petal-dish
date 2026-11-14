// How the console reads an event's new fields (UX-1, UX-3, UX-6, UX-9, K18).
import { describe, expect, it } from 'vitest';
import type { EvidenceRow, MeasurementRow, SolutionHistoryRow, SolutionOut, SolutionSummary, SystemConfig } from '../api/types.gen';
import { formatLat, formatLon } from '../format/geo';
import { candidates, distanceText, doubtCautionText, ephemerisDoubt, factorText, growthText, isInconsistent, isNotFix, notFixText, received, semiAxes, splitHistory, summaryCandidatesText, summaryReceivedText, widthText } from './event';

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

describe('the fix gate (R4)', () => {
  it('is not a fix when the server says so, or when the fit is inconsistent', () => {
    expect(isNotFix(sol())).toBe(false);
    expect(isNotFix(sol({ is_fix: true }))).toBe(false);
    expect(isNotFix(sol({ is_fix: false, not_fix_reason: 'no redundancy' }))).toBe(true);
    // a server older than is_fix: the inconsistent flag alone still means no fix
    expect(isNotFix(sol({ fit_consistent: false }))).toBe(true);
    expect(isNotFix(null)).toBe(false);
    const row: SolutionHistoryRow = { t_ns: '1', semi_major_m: 1, semi_minor_m: 1, n_measurements: 2, is_fix: false };
    expect(isNotFix(row)).toBe(true);
  });
  it("gives the server's reason as a sentence", () => {
    const reason = 'no redundancy: 2 measurements for the two unknowns of a position, so the fit cannot be checked against itself';
    expect(notFixText(sol({ is_fix: false, not_fix_reason: reason }))).toBe(`No redundancy: 2 measurements for the two unknowns of a position, so the fit cannot be checked against itself.`);
    expect(notFixText(sol())).toBeNull();
    expect(notFixText(sol({ fit_consistent: false }))).toBe('The measurements do not fit one stationary transmitter.');
  });
});

describe('width of a range (ADAPT-5)', () => {
  it('says "at least" when every detection filled its window', () => {
    expect(widthText(301.001e6, 301.009e6, false)).toBe('8.00 kHz');
    expect(widthText(301.001e6, 301.009e6, true)).toBe('at least 8.00 kHz');
    expect(widthText(301.001e6, 301.009e6, undefined)).toBe('8.00 kHz');
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

describe('ellipse widened for ephemeris doubt (closing D1)', () => {
  const ch = (sat: string, site: string) => ({ sat_id: sat, site_id: site, channel_id: `${site}-${sat}` });
  const row = (a: string, b: string, residual: number | null): MeasurementRow => ({
    t_ns: '1', kind: 'tdoa', pair: { a: ch(a, 'naha'), b: ch(b, 'naha') }, value: 1e-3, sigma: 1e-8, residual, snr_db: 20, calibrated: true, used: residual !== null, reason: null,
  });
  const rows = [row('muos-pac', 'uhf-172e', 0.4), row('muos-io', 'uhf-172e', null)];
  it('writes factors and growth as the server does', () => {
    expect([9.51, 3.36, 8, 12.3, 20].map(factorText)).toEqual(['9.5', '3.4', '8', '12', '20']);
    // the major axis, not the area: SolutionOut.ellipse_widening is a ratio of semi-major axes
    expect(growthText(1.214)).toBe('21 % longer than with the configured uncertainty');
    expect(growthText(1.004)).toBe('less than 1 % longer than with the configured uncertainty');
    expect(growthText(0.998)).toBe('less than 1 % longer than with the configured uncertainty');
    expect(growthText(2.43)).toBe('2.4 times as long as with the configured uncertainty');
    expect(growthText(null)).toBeNull();
  });
  it('writes a widened sigma as the server advice and status do (advisory.format_distance)', () => {
    expect([850, 8000, 9510, 12340, 1250000].map(distanceText)).toEqual(['850 m', '8.0 km', '9.5 km', '12 km', '1,250 km']);
  });
  it('lists the widened satellites with their sigma and the growth of the ellipse', () => {
    const solution = sol({
      ephemeris_allowance: [
        { sat_id: 'muos-pac', scale: 8, sigma_ric_m: [800, 8000, 8000], capped: false },
        { sat_id: 'uhf-172e', scale: 20, sigma_ric_m: [2000, 20000, 20000], capped: true },
      ],
      ellipse_widening: 1.214,
    });
    expect(ephemerisDoubt({ solution, measurements: rows, doubted_satellite_ids: ['muos-pac', 'uhf-172e'] })).toEqual({
      widened: [
        { sat: 'muos-pac', factor: '8', sigmaM: 8000, capped: false },
        { sat: 'uhf-172e', factor: '20', sigmaM: 20000, capped: true },
      ],
      growth: '21 % longer than with the configured uncertainty',
      notYet: [],
    });
    expect(doubtCautionText(ephemerisDoubt({ solution, measurements: rows, doubted_satellite_ids: [] }))).toBe(
      'The reference calibrations put the position of uhf-172e wrong by more than the largest allowance the system makes, or by an amount they cannot size; the ellipse includes that largest allowance, but the transmitter may still lie outside it; check its ephemeris.',
    );
    // widened within the largest scale: the ellipse covers it, no caution
    const covered = sol({ ephemeris_allowance: [{ sat_id: 'muos-pac', scale: 8, sigma_ric_m: [800, 8000, 8000] }], ellipse_widening: null });
    const d = ephemerisDoubt({ solution: covered, measurements: rows, doubted_satellite_ids: ['muos-pac'] });
    expect(d).toEqual({ widened: [{ sat: 'muos-pac', factor: '8', sigmaM: 8000, capped: false }], growth: null, notYet: [] });
    expect(doubtCautionText(d)).toBeNull();
  });
  it('names a doubted satellite the rows go through that the solve did not widen for yet, and nothing else', () => {
    const solution = sol({ ephemeris_allowance: [], ellipse_widening: null });
    // muos-io is in doubt but no row of the solve goes through it; uhf-172e is
    const d = ephemerisDoubt({ solution, measurements: rows, doubted_satellite_ids: ['muos-io', 'uhf-172e'] });
    expect(d).toEqual({ widened: [], growth: null, notYet: ['uhf-172e'] });
    expect(doubtCautionText(d)).toBe(
      'The reference calibrations put the position of uhf-172e wrong beyond its configured uncertainty, and the ellipse does not include that error yet: the transmitter may lie outside it.',
    );
    expect(doubtCautionText({ widened: [], growth: null, notYet: ['muos-pac', 'uhf-172e'] })).toContain('positions of muos-pac and uhf-172e wrong beyond their configured');
    expect(ephemerisDoubt({ solution, measurements: rows, doubted_satellite_ids: ['muos-io'] })).toBeNull();
    expect(ephemerisDoubt({ solution: sol(), measurements: rows })).toBeNull();
    expect(ephemerisDoubt({ solution: null, measurements: rows, doubted_satellite_ids: ['uhf-172e'] })).toBeNull();
  });
});
