// What an event's fields mean for the operator: whether a solution is a fix, every position
// of an unresolved ambiguity, what a local signal is received on, where the ellipse
// history broke off, and live against "as of" wording. Pure functions, so the screens
// and the tests share them.
import type { Event, EventSummary, SolutionHistoryRow, SolutionOut, SolutionSummary, SystemConfig } from '../api/types.gen';
import { formatLat, formatLon } from '../format/geo';
import { fixed, km, mhz } from '../format/units';

/** A solution the server flagged inconsistent (SolutionOut.fit_consistent false): never a fix. */
export function isInconsistent(sol: Pick<SolutionOut | SolutionSummary, 'fit_consistent'> | null | undefined): boolean {
  return !!sol && sol.fit_consistent === false;
}

/**
 * A solution that may not be reported as a fix (R4): the server says so (`is_fix` false: no
 * redundancy, an ellipse beyond its sanity bound, no convergence) or the fit is inconsistent
 * (a server older than `is_fix` sets only `fit_consistent`). Never shown as a position.
 */
export function isNotFix(sol: Pick<SolutionOut | SolutionSummary | SolutionHistoryRow, 'fit_consistent' | 'is_fix'> | null | undefined): boolean {
  return !!sol && (sol.is_fix === false || sol.fit_consistent === false);
}

/** Why a solution is not a fix, as a sentence ("No redundancy: ..."), or null for a fix. */
export function notFixText(sol: Pick<SolutionOut, 'fit_consistent' | 'is_fix' | 'not_fix_reason'> | null | undefined): string | null {
  if (!sol || !isNotFix(sol)) return null;
  const reason = sol.not_fix_reason?.trim();
  if (!reason) return sol.fit_consistent === false ? 'The measurements do not fit one stationary transmitter.' : 'The server does not count it as a fix.';
  return `${reason.charAt(0).toUpperCase()}${reason.slice(1)}${/[.?]$/.test(reason) ? '' : '.'}`;
}

/** Width of a range: "8.00 kHz", or "at least 8.00 kHz" when every detection filled its monitor window (ADAPT-5). */
export function widthText(loHz: number, hiHz: number, lowerBound: boolean | null | undefined): string {
  const w = `${fixed((hiHz - loHz) / 1000, 2)} kHz`;
  return lowerBound ? `at least ${w}` : w;
}

export interface Candidate {
  lat: number;
  lon: number;
  semiMajorM: number;
  semiMinorM: number;
  /** chi-square above the best candidate's (0 for the best) */
  dChi2: number | null;
}

/**
 * Every position that fits as well when the ambiguity is unresolved (the solution and each
 * of its ambiguities, best first); empty when the solution is unique or the ambiguity was
 * resolved.
 */
export function candidates(sol: SolutionOut | null | undefined): Candidate[] {
  if (!sol || sol.ambiguity_resolved_by !== 'unresolved' || !sol.ambiguities.length) return [];
  return [
    { lat: sol.lat_deg, lon: sol.lon_deg, semiMajorM: sol.ellipse.semi_major_m, semiMinorM: sol.ellipse.semi_minor_m, dChi2: 0 },
    ...sol.ambiguities.map((a) => ({ lat: a.lat_deg, lon: a.lon_deg, semiMajorM: a.ellipse.semi_major_m, semiMinorM: a.ellipse.semi_minor_m, dChi2: a.chi2 - sol.chi2 })),
  ];
}

/** Queue text for a summary's ambiguity: "one of 2 positions: 20.31° N 132.42° E or 5.12° S 133.20° E". */
export function summaryCandidatesText(sol: SolutionSummary): string | null {
  if (!sol.ambiguous) return null;
  const all = [{ lat: sol.lat_deg, lon: sol.lon_deg }, ...(sol.ambiguities ?? []).map((a) => ({ lat: a.lat_deg, lon: a.lon_deg }))];
  const n = Math.max(2, all.length);
  const shown = all.slice(0, 2).map((p) => `${formatLat(p.lat, 2)} ${formatLon(p.lon, 2)}`);
  const more = all.length > 2 ? ` and ${all.length - 2} more` : '';
  return `one of ${n} positions: ${shown.join(' or ')}${more}`;
}

/** "0.46 × 0.06 km" (the 95 % semi-axes; the one convention on every screen). */
export function semiAxes(semiMajorM: number, semiMinorM: number): string {
  return `${km(semiMajorM)} × ${km(semiMinorM)} km`;
}

export interface Received {
  /** "260.0000–260.0100 MHz" */
  range: string;
  /** site names that see it */
  sites: string[];
  /** channel ids that see it, with the satellite each watches */
  channels: Array<{ channel: string; sat: string | null }>;
}

const siteNameOf = (cfg: SystemConfig | null, id: string) => cfg?.sites.find((s) => s.id === id)?.name.replace(/\s*\(.*\)$/, '') ?? id;

/**
 * What a signal is received on: the downlink range of the "seen" evidence rows, the sites
 * and channels that see it. For a local signal this is what someone hunting the source
 * tunes to (the uplink equivalent means nothing there). Null when nothing is seen.
 */
export function received(e: Pick<Event, 'evidence' | 'received_lo_hz' | 'received_hi_hz' | 'seen_site_ids'>, cfg: SystemConfig | null): Received | null {
  const seen = e.evidence.filter((r) => r.state === 'seen');
  let lo = e.received_lo_hz ?? null;
  let hi = e.received_hi_hz ?? null;
  if (lo === null || hi === null) {
    for (const r of seen) {
      if (r.downlink_lo_hz === null || r.downlink_lo_hz === undefined || r.downlink_hi_hz === null || r.downlink_hi_hz === undefined) continue;
      lo = lo === null ? r.downlink_lo_hz : Math.min(lo, r.downlink_lo_hz);
      hi = hi === null ? r.downlink_hi_hz : Math.max(hi, r.downlink_hi_hz);
    }
  }
  if (lo === null || hi === null) return null;
  const siteIds = e.seen_site_ids?.length ? e.seen_site_ids : [...new Set(seen.map((r) => r.site_id))];
  return {
    range: `${mhz(lo, 4)}–${mhz(hi, 4)} MHz`,
    sites: siteIds.map((s) => siteNameOf(cfg, s)),
    channels: seen.map((r) => ({ channel: r.channel_id, sat: r.sat_id ?? null })),
  };
}

/** Queue line for a local event: "260.0000–260.0100 MHz received at Busan". */
export function summaryReceivedText(e: Pick<EventSummary, 'received_lo_hz' | 'received_hi_hz' | 'seen_site_ids'>, cfg: SystemConfig | null): string | null {
  if (e.received_lo_hz === null || e.received_lo_hz === undefined || e.received_hi_hz === null || e.received_hi_hz === undefined) return null;
  const sites = (e.seen_site_ids ?? []).map((s) => siteNameOf(cfg, s));
  return `${mhz(e.received_lo_hz, 4)}–${mhz(e.received_hi_hz, 4)} MHz received${sites.length ? ` at ${sites.join(', ')}` : ''}`;
}

export interface HistorySplit {
  /** solves whose measurements were later set aside (a break, another transmitter, a local signal) */
  setAside: SolutionHistoryRow[];
  /** solves since the newest set-aside */
  current: SolutionHistoryRow[];
  /** time of the first solve after the break, or null when nothing was set aside */
  breakNs: string | null;
}

/** Where the ellipse history broke off: rows set aside before, the current transmission's after. */
export function splitHistory(rows: SolutionHistoryRow[]): HistorySplit {
  const setAside = rows.filter((r) => r.set_aside === true);
  const current = rows.filter((r) => r.set_aside !== true);
  return { setAside, current, breakNs: setAside.length && current.length ? (current[0]?.t_ns ?? null) : null };
}

/** A satellite whose position sigma the published solve widened (SolutionOut.ephemeris_allowance). */
export interface WidenedSatellite {
  sat: string;
  /** "9.5" or "12": the factor on its configured position sigma, as the server writes it */
  factor: string;
  /** the widened one-sigma, its largest component (radial, in-track, cross-track), m */
  sigmaM: number;
  /** at the largest scale allowed, or not sized: the ellipse may still not cover the error */
  capped: boolean;
}

export interface EphemerisDoubt {
  /** satellites the solve widened for, in the server's order (by id) */
  widened: WidenedSatellite[];
  /** how much longer the 95 % ellipse's major axis is for it (growthText); null when not known */
  growth: string | null;
  /** satellites in doubt (Event.doubted_satellite_ids) the solution's rows go through that the solve did not widen for yet */
  notYet: string[];
}

/** An allowance's factor as the server writes it (events.scale_text): "9.5", "3.4", "12". */
export function factorText(scale: number): string {
  return scale < 10 ? String(Number(scale.toPrecision(2))) : String(Math.round(scale));
}

/**
 * How much an allowance lengthened the 95 % ellipse's major axis (SolutionOut.ellipse_widening is a
 * ratio of semi-major axes), as the server writes it (events.widening_text): "21 % longer than with
 * the configured uncertainty", "2.4 times as long as with the configured uncertainty"; null when
 * unknown.
 */
export function growthText(widening: number | null | undefined): string | null {
  if (widening === null || widening === undefined || !Number.isFinite(widening) || widening <= 0) return null;
  if (widening >= 2) return `${Number(widening.toPrecision(2))} times as long as with the configured uncertainty`;
  const p = (widening - 1) * 100;
  return `${p < 1 ? 'less than 1 %' : `${Math.round(p)} %`} longer than with the configured uncertainty`;
}

/**
 * A widened one-sigma as the server's advice and status write it (advisory.format_distance):
 * "850 m", "8.0 km", "12 km", "1,250 km".
 */
export function distanceText(meters: number): string {
  if (!Number.isFinite(meters)) return 'an unknown distance';
  if (meters < 1000) return `${Math.round(meters)} m`;
  const k = meters / 1000;
  return k < 10 ? `${k.toFixed(1)} km` : `${Math.round(k).toLocaleString('en-US')} km`;
}

/**
 * What the published solution says about satellites the reference calibrations put wrong
 * beyond their configured uncertainty (D1): the ones its ellipse is widened for, by how much,
 * and those in doubt that its rows go through but the solve did not widen for yet (the
 * doubt came after it, or is not confirmed yet: then the server holds a first fix back as
 * not a fix). Null when there is no solution or nothing to say.
 */
export function ephemerisDoubt(e: Pick<Event, 'solution' | 'measurements' | 'doubted_satellite_ids'>): EphemerisDoubt | null {
  const sol = e.solution;
  if (!sol) return null;
  const widened = (sol.ephemeris_allowance ?? []).map((a) => ({
    sat: a.sat_id,
    factor: factorText(a.scale),
    sigmaM: Math.max(...a.sigma_ric_m),
    capped: a.capped === true,
  }));
  // the rows of the published solve are the ones with a residual (MeasurementRow.residual)
  const through = new Set<string>();
  for (const m of e.measurements) {
    if (m.residual === null || m.residual === undefined) continue;
    through.add(m.pair.a.sat_id);
    through.add(m.pair.b.sat_id);
  }
  const done = new Set(widened.map((w) => w.sat));
  const notYet = (e.doubted_satellite_ids ?? []).filter((s) => through.has(s) && !done.has(s));
  if (!widened.length && !notYet.length) return null;
  return { widened, growth: widened.length ? growthText(sol.ellipse_widening) : null, notYet };
}

const andList = (items: string[]): string => (items.length < 2 ? (items[0] ?? '') : `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}`);

/**
 * The caution for an ephemeris doubt the ellipse may not cover (D1): satellites whose
 * allowance is at the largest scale allowed, and satellites in doubt that the solve did not
 * widen for yet. Null when the widened ellipse covers every doubt.
 */
export function doubtCautionText(d: EphemerisDoubt | null): string | null {
  if (!d) return null;
  const parts: string[] = [];
  const capped = d.widened.filter((w) => w.capped).map((w) => w.sat);
  if (capped.length) {
    // capped: the error asks for more than the largest scale, or the calibrations cannot size it
    const one = capped.length === 1;
    parts.push(
      `The reference calibrations put the ${one ? 'position' : 'positions'} of ${andList(capped)} wrong by more than the largest allowance the system makes, or by ${one ? 'an amount' : 'amounts'} they cannot size; the ellipse includes that largest allowance, but the transmitter may still lie outside it; check ${one ? 'its ephemeris' : 'their ephemerides'}.`,
    );
  }
  if (d.notYet.length) {
    const one = d.notYet.length === 1;
    parts.push(
      `The reference calibrations put the ${one ? 'position' : 'positions'} of ${andList(d.notYet)} wrong beyond ${one ? 'its' : 'their'} configured uncertainty, and the ellipse does not include that error yet: the transmitter may lie outside it.`,
    );
  }
  return parts.length ? parts.join(' ') : null;
}
