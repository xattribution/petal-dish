// What an event's fields mean for the operator: whether a solution is a fix, every position
// of an unresolved ambiguity, what a local signal is received on, where the ellipse
// history broke off, and live against "as of" wording. Pure functions, so the screens
// and the tests share them.
import type { Event, EventSummary, SolutionHistoryRow, SolutionOut, SolutionSummary, SystemConfig } from '../api/types.gen';
import { formatLat, formatLon } from '../format/geo';
import { km, mhz } from '../format/units';

/** A solution the server flagged inconsistent (SolutionOut.fit_consistent false): never a fix. */
export function isInconsistent(sol: Pick<SolutionOut | SolutionSummary, 'fit_consistent'> | null | undefined): boolean {
  return !!sol && sol.fit_consistent === false;
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
