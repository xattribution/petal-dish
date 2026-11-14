// Astro status ladder and how PETAL Locate states map onto it (DESIGN.md ledger).
import type { ChannelStatus, EventSummary, NodeStatus, SatelliteStatus, SiteClockEstimate } from '../api/types.gen';
import { formatAge } from '../time/dtg';
import { fixed } from '../format/units';

export type Level = 'off' | 'standby' | 'normal' | 'caution' | 'serious' | 'critical';
export const LEVELS: readonly Level[] = ['off', 'standby', 'normal', 'caution', 'serious', 'critical'];
export const LEVEL_WORD: Record<Level, string> = {
  off: 'Off',
  standby: 'Standby',
  normal: 'Normal',
  caution: 'Caution',
  serious: 'Serious',
  critical: 'Critical',
};

export function rank(l: Level): number {
  return LEVELS.indexOf(l);
}

export function worst(levels: Iterable<Level>, fallback: Level = 'normal'): Level {
  let w: Level | null = null;
  for (const l of levels) if (w === null || rank(l) > rank(w)) w = l;
  return w ?? fallback;
}

export function isActive(e: { state: string }): boolean {
  return e.state !== 'lost' && e.state !== 'closed';
}

/** Uplink affects every user of the transponder (Critical); local one site (Serious). */
export function eventLevel(e: Pick<EventSummary, 'state' | 'verdict'>): Level {
  if (e.state === 'closed') return 'off';
  if (e.state === 'lost') return 'standby';
  if (e.verdict === 'uplink') return 'critical';
  if (e.verdict === 'local') return 'serious';
  return 'caution';
}

export const VERDICT_WORD: Record<string, string> = { uplink: 'Uplink', local: 'Local', undetermined: 'Undetermined' };
export const STATE_WORD: Record<string, string> = {
  detected: 'detected',
  characterizing: 'characterizing',
  locating: 'locating',
  located: 'located',
  lost: 'lost',
  closed: 'closed',
};

/** Queue order: active before inactive, then most urgent, then most recently seen. */
export function compareEvents(a: EventSummary, b: EventSummary): number {
  const act = Number(isActive(b)) - Number(isActive(a));
  if (act) return act;
  const lv = rank(eventLevel(b)) - rank(eventLevel(a));
  if (lv) return lv;
  const ta = BigInt(a.last_seen_ns);
  const tb = BigInt(b.last_seen_ns);
  return tb > ta ? 1 : tb < ta ? -1 : a.id.localeCompare(b.id);
}

export interface NodeAssessment {
  level: Level;
  /** short reasons, worst first */
  reasons: string[];
}

/** "−10.05 µs" from a site clock estimate's offset in microseconds. */
export function siteClockText(c: Pick<SiteClockEstimate, 'offset_us'>): string {
  const a = Math.abs(c.offset_us);
  return a >= 1000 ? `${fixed(c.offset_us / 1000, 2)} ms` : a >= 1 ? `${fixed(c.offset_us, 2)} µs` : `${fixed(c.offset_us * 1000, 0)} ns`;
}

/** Windows of a channel that detection cannot use: blind (frames refused) or a raised noise floor. */
export function windowProblems(ch: ChannelStatus): { blind: string[]; floorRaised: Array<{ id: string; db: number }> } {
  const blind: string[] = [];
  const floorRaised: Array<{ id: string; db: number }> = [];
  for (const w of ch.windows ?? []) {
    if (w.blind) blind.push(w.monitor_id);
    if (w.floor_raised_db !== null && w.floor_raised_db !== undefined) floorRaised.push({ id: w.monitor_id, db: w.floor_raised_db });
  }
  return { blind, floorRaised };
}

/**
 * A node is Serious when offline; Caution when its clock is unlocked, the reference
 * calibrations put its clock off (Status.site_clocks caution), a channel failed, a
 * window is blind to detection or has a raised noise floor (a signal may fill it), or a
 * channel's spectrum is stale.
 */
export function nodeLevel(n: NodeStatus, opts: { siteClock?: SiteClockEstimate | null; staleAfterS?: number } = {}): NodeAssessment {
  if (!n.online) return { level: 'serious', reasons: ['offline'] };
  const staleAfterS = opts.staleAfterS ?? 1.5;
  const reasons: string[] = [];
  if (n.clock && !n.clock.locked) reasons.push(`clock unlocked (${n.clock.source}, holdover ${formatAge(n.clock.holdover_s)})`);
  if (!n.clock) reasons.push('no clock report');
  if (opts.siteClock?.caution) reasons.push(`clock about ${siteClockText(opts.siteClock)} off by reference calibration`);
  let blind = 0;
  let raised = 0;
  let failed = 0;
  for (const c of n.channels) {
    const p = windowProblems(c);
    blind += p.blind.length;
    raised += p.floorRaised.length;
    if (!c.ok && p.blind.length === 0) failed += 1;
  }
  if (failed) reasons.push(`${failed} channel${failed > 1 ? 's' : ''} failed`);
  if (blind) reasons.push(`detection blind on ${blind} window${blind > 1 ? 's' : ''}`);
  if (raised) reasons.push(`noise floor raised on ${raised} window${raised > 1 ? 's' : ''}`);
  const stale = n.channels.filter((c) => c.ok && (c.last_frame_age_s ?? Infinity) > staleAfterS);
  if (stale.length) reasons.push(`${stale.length} channel${stale.length > 1 ? 's' : ''} with stale spectrum`);
  return { level: reasons.length ? 'caution' : 'normal', reasons };
}

/** Satellites the server cannot place now (Status.satellites): Serious, nothing is located through them. */
export function satelliteProblems(sats: SatelliteStatus[] | undefined): SatelliteStatus[] {
  return (sats ?? []).filter((s) => !s.ephemeris_ok);
}

export interface Rollup {
  level: Level;
  /** the status word next to the symbol */
  word: string;
  /** false: no current data (never received, or the link is down or the status old) */
  current: boolean;
}

/**
 * The header roll-up. With no status ever received it is Off, "No data" (never a green
 * all-clear); while the link is down or the status is old it keeps the last level but
 * says it is not current; otherwise the most urgent of nodes, satellites and active events.
 */
export function rollup(input: { status: { nodes: NodeStatus[]; satellites?: SatelliteStatus[]; site_clocks?: SiteClockEstimate[] } | null; live: boolean; events: Iterable<Pick<EventSummary, 'state' | 'verdict'>> }): Rollup {
  if (!input.status) return { level: 'off', word: 'No data', current: false };
  const clocks = new Map((input.status.site_clocks ?? []).map((c) => [c.site_id, c]));
  const ls: Level[] = [];
  for (const n of input.status.nodes) ls.push(nodeLevel(n, { siteClock: clocks.get(n.site_id) }).level);
  if (satelliteProblems(input.status.satellites).length) ls.push('serious');
  for (const e of input.events) if (isActive(e)) ls.push(eventLevel(e));
  const level = worst(ls, 'normal');
  return { level, word: LEVEL_WORD[level], current: input.live };
}
