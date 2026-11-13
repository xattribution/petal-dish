// Astro status ladder and how PETAL Locate states map onto it (DESIGN.md ledger).
import type { EventSummary, NodeStatus } from '../api/types.gen';

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

/** A node is Serious when offline, Caution when its clock is unlocked or a channel is stale or failing. */
export function nodeLevel(n: NodeStatus, staleAfterS = 1.5): NodeAssessment {
  if (!n.online) return { level: 'serious', reasons: ['offline'] };
  const reasons: string[] = [];
  if (n.clock && !n.clock.locked) reasons.push(`clock unlocked (${n.clock.source}, holdover ${Math.round(n.clock.holdover_s)} s)`);
  if (!n.clock) reasons.push('no clock report');
  const bad = n.channels.filter((c) => !c.ok);
  if (bad.length) reasons.push(`${bad.length} channel${bad.length > 1 ? 's' : ''} failed`);
  const stale = n.channels.filter((c) => c.ok && (c.last_frame_age_s ?? Infinity) > staleAfterS);
  if (stale.length) reasons.push(`${stale.length} channel${stale.length > 1 ? 's' : ''} with stale spectrum`);
  return { level: reasons.length ? 'caution' : 'normal', reasons };
}
