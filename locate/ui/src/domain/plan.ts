// Accuracy-map planning from the loaded configuration (ADAPT-10, ADAPT-4): the pair ends a
// map accepts, and a starting request derived from the satellites, sites and bands
// configured, never from one example deployment.
import type { AccuracyPairSummary, SystemConfig } from '../api/types.gen';
import { groupInt, MINUS } from '../format/units';
import { watchedSatellites } from './config';

/**
 * `AccuracyRequest.reference_carrier_id` that calibrates each cell's pairs against the
 * reference carrier tasking would use for an emitter there (core.accuracy.NEAREST_REFERENCE).
 */
export const NEAREST_REFERENCE = 'nearest';

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

/**
 * The reference the accuracy form starts with: "nearest" when the configuration has
 * reference carriers (live tasking always calibrates each pair against the nearest
 * eligible reference, so the map then predicts what tasking does), else none ('').
 */
export function defaultReference(cfg: SystemConfig): string {
  return (cfg.carriers ?? []).some((c) => c.use_as_reference) ? NEAREST_REFERENCE : '';
}

/** Every reference value the form offers ('' is none); "nearest" only when a reference exists and no carrier is named so. */
export function referenceValues(cfg: SystemConfig): string[] {
  const refs = (cfg.carriers ?? []).filter((c) => c.use_as_reference).map((c) => c.id);
  return ['', ...(refs.length && !refs.includes(NEAREST_REFERENCE) ? [NEAREST_REFERENCE] : []), ...refs];
}

/**
 * The reference the form shows: the operator's choice once they have made one and the
 * configuration still offers it, else the configuration's default (it follows a reload).
 */
export function referenceChoice(cfg: SystemConfig, chosen: string | null): string {
  return chosen !== null && referenceValues(cfg).includes(chosen) ? chosen : defaultReference(cfg);
}

/** Key of `AccuracyPairSummary.reference_cells` for cells with no usable reference. */
export const NO_REFERENCE_KEY = 'none';

export interface PairCalibrationView {
  /** "tokyo-pac − tokyo-io" */
  ends: string;
  /** "muos-pac − muos-io", null when the pair could not be resolved */
  satellites: string | null;
  /** what calibrates the pair, in words */
  calibration: string;
  /** plain warning when some or all cells have no usable reference (only when a reference was asked for) */
  uncalibrated: string | null;
  /** why the pair gives nothing at all */
  unusable: string | null;
  notes: string[];
}

const KIND_WORD: Record<string, string> = { tdoa: 'TDOA', fdoa: 'FDOA' };
const count = (n: number) => groupInt(String(Math.round(n)));

function sentence(text: string): string {
  return /[.!?]$/.test(text) ? text : `${text}.`;
}

/**
 * One pair of a computed accuracy map in words (closing D2): its two ends and their
 * satellites, what it is calibrated with, and a plain warning, with the server's reason,
 * when some or all of the grid's `totalCells` cells have no usable reference.
 */
export function pairCalibration(p: AccuracyPairSummary, totalCells: number): PairCalibrationView {
  const ends = `${p.a_channel} ${MINUS} ${p.b_channel}`;
  const satellites = p.a_sat_id && p.b_sat_id ? `${p.a_sat_id} ${MINUS} ${p.b_sat_id}` : null;
  const cells = p.reference_cells ?? {};
  const kinds = (p.calibrated ?? []).map((k) => KIND_WORD[k] ?? k).join(', ');
  const asked = Object.keys(cells).length > 0;
  const unc = p.uncalibrated_cells ?? (asked ? 0 : totalCells);
  let calibration: string;
  if (!asked) calibration = 'none (no reference emitter chosen)';
  else if (p.reference_carrier_id) calibration = kinds ? `${p.reference_carrier_id} (${kinds})` : `none (${p.reference_carrier_id} cannot calibrate it)`;
  else {
    const chosen = Object.entries(cells)
      .filter(([id, n]) => id !== NO_REFERENCE_KEY && n > 0)
      .sort((a, b) => b[1] - a[1]);
    calibration = chosen.length
      ? `nearest reference: ${chosen.map(([id, n]) => (n === totalCells ? `${id} in every cell` : `${id} in ${count(n)} cells`)).join(', ')}${kinds ? ` (${kinds})` : ''}`
      : 'none (no eligible reference carrier)';
  }
  let uncalibrated: string | null = null;
  if (asked && unc > 0 && !p.reason) {
    const where = unc >= totalCells ? `All ${count(totalCells)} cells are uncalibrated` : `${count(unc)} of ${count(totalCells)} cells are uncalibrated`;
    uncalibrated = p.reference_reason ? sentence(`${where}: ${p.reference_reason}`) : `${where}.`;
  }
  return { ends, satellites, calibration, uncalibrated, unusable: p.reason ? sentence(`This pair gives no measurement: ${p.reason}`) : null, notes: p.notes ?? [] };
}
