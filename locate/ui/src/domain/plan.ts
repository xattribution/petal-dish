// Accuracy-map planning from the loaded configuration (ADAPT-10, ADAPT-4): the pair ends a
// map accepts, and a starting request derived from the satellites, sites and bands
// configured, never from one example deployment.
import type { SystemConfig } from '../api/types.gen';
import { watchedSatellites } from './config';

export interface PairEnd {
  /** what the request carries: "channel_id", or "channel_id@sat_id" for a channel watching several satellites */
  value: string;
  channel: string;
  sat: string;
  site: string;
}

/**
 * Every pair end an accuracy map accepts. A channel watching one satellite is one end; a
 * channel watching several (a fixed or wide-beam pointing) gives one "channel@sat" end per
 * satellite, since which satellite a pair means must be said.
 */
export function pairEnds(cfg: SystemConfig): PairEnd[] {
  const out: PairEnd[] = [];
  for (const site of cfg.sites)
    for (const ch of site.channels) {
      const sats = watchedSatellites(ch);
      for (const sat of sats) out.push({ value: sats.length > 1 ? `${ch.id}@${sat}` : ch.id, channel: ch.id, sat, site: site.id });
    }
  return out;
}

export interface AccuracyDefaults {
  pairs: Array<[string, string]>;
  /** uplink frequency, Hz, inside the band the paired satellites all relay (null: no such pair) */
  feHz: number | null;
  /** occupied bandwidth, Hz */
  bwHz: number;
}

const BW_FRACTION = 0.32; // of the common band: 8 kHz in a 25 kHz UHF channel
const BW_MIN_HZ = 1e3;
const BW_MAX_HZ = 5e6;
const MAX_PAIRS = 2; // the tasking rule: two pairs sharing one channel give TDOA and FDOA through three satellites

/**
 * A starting accuracy request for any configuration: the uplink band most satellites relay
 * in common, the site whose channels watch the most of those satellites (same-site pairs
 * share a clock), up to two pairs sharing that site's last channel, a frequency at the
 * center of the common band and a bandwidth scaled to it. Falls back to a cross-site pair,
 * and to no pair when no two channels on different satellites relay a common frequency.
 */
export function accuracyDefaults(cfg: SystemConfig): AccuracyDefaults {
  const ends = pairEnds(cfg);
  let best: { pairs: Array<[string, string]>; nSats: number; lo: number; hi: number } | null = null;
  for (const sat of cfg.satellites)
    for (const tp of sat.transponders) {
      // satellites relaying a common part of this transponder's uplink band
      let lo = tp.uplink_center_hz - tp.bandwidth_hz / 2;
      let hi = tp.uplink_center_hz + tp.bandwidth_hz / 2;
      const group = [sat.id];
      for (const other of cfg.satellites) {
        if (other.id === sat.id) continue;
        let bestOverlap: [number, number] | null = null;
        for (const t of other.transponders) {
          const l = Math.max(lo, t.uplink_center_hz - t.bandwidth_hz / 2);
          const h = Math.min(hi, t.uplink_center_hz + t.bandwidth_hz / 2);
          if (h > l && (!bestOverlap || h - l > bestOverlap[1] - bestOverlap[0])) bestOverlap = [l, h];
        }
        if (bestOverlap) {
          group.push(other.id);
          [lo, hi] = bestOverlap;
        }
      }
      if (group.length < 2) continue;
      const candidates: Array<{ pairs: Array<[string, string]>; nSats: number }> = [];
      for (const site of cfg.sites) {
        // one end per satellite of the group at this site, preferring a channel that watches
        // only that satellite (it can pair with a multi-satellite channel), in configuration order
        const atSite = ends.filter((e) => e.site === site.id && group.includes(e.sat));
        const multi = (e: PairEnd) => e.value.includes('@');
        const chosen = new Map<string, PairEnd>();
        for (const e of atSite) {
          const prev = chosen.get(e.sat);
          if (!prev || (multi(prev) && !multi(e))) chosen.set(e.sat, e);
        }
        const here = atSite.filter((e) => chosen.get(e.sat) === e);
        const last = here.at(-1);
        if (!last) continue;
        const pairs = here
          .slice(0, -1)
          .filter((e) => e.channel !== last.channel)
          .slice(0, MAX_PAIRS)
          .map((e) => [e.value, last.value] as [string, string]);
        if (pairs.length) candidates.push({ pairs, nSats: here.length });
      }
      if (!candidates.length) {
        // no site has two channels on the group: a pair across sites
        const a = ends.find((e) => group.includes(e.sat));
        const b = a ? ends.find((e) => group.includes(e.sat) && e.sat !== a.sat && e.channel !== a.channel) : undefined;
        if (a && b) candidates.push({ pairs: [[a.value, b.value]], nSats: 2 });
      }
      for (const c of candidates) {
        const better = !best || c.pairs.length > best.pairs.length || (c.pairs.length === best.pairs.length && c.nSats > best.nSats);
        if (better) best = { ...c, lo, hi };
      }
    }
  if (!best) return { pairs: [], feHz: null, bwHz: 8e3 };
  const width = best.hi - best.lo;
  return { pairs: best.pairs, feHz: Math.round((best.lo + best.hi) / 2), bwHz: Math.min(BW_MAX_HZ, Math.max(BW_MIN_HZ, Math.round(width * BW_FRACTION))) };
}
