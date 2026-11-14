// Configuration helpers the screens share: lookups, derived monitor windows (the rule of
// config/models.py derive_monitors, so channels without explicit monitors still show
// their windows), and the electrically-small antenna caution.
import type { Monitor, ParabolicAntenna, ReceiveChannel, Satellite, Site, SystemConfig, Transponder } from '../api/types.gen';

export const C_LIGHT = 299792458;
const DEFAULT_NFFT = 1024;
const DEFAULT_NAVG = 8;
const DEFAULT_RATE = 2.0;
const MIN_NFFT = 64;
const SPAN_FACTOR = 1.25;

export function watchedSatellites(ch: ReceiveChannel): string[] {
  return 'satellite_id' in ch.pointing ? [ch.pointing.satellite_id] : [...ch.pointing.satellite_ids];
}

export function downlinkCenter(tp: Transponder): number {
  return tp.uplink_center_hz + tp.translation_hz;
}

/** (nfft, navg) reduced until a frame of nfft * navg samples at span_hz fits the frame period. */
export function fitMonitorFrame(spanHz: number, frameRateHz = DEFAULT_RATE, nfft0 = DEFAULT_NFFT, navg0 = DEFAULT_NAVG): [number, number] {
  const budget = (spanHz * (1 + 1e-12)) / frameRateHz;
  let nfft = nfft0;
  let navg = Math.max(1, Math.min(navg0, Math.floor(budget / nfft)));
  while (nfft * navg > budget && nfft / 2 >= MIN_NFFT) nfft /= 2;
  if (nfft * navg > budget) throw new Error(`span_hz ${spanHz} is too narrow for ${frameRateHz} frames per second`);
  return [nfft, navg];
}

/** One window per transponder of each watched satellite, as the server derives them. */
export function deriveMonitors(ch: ReceiveChannel, satellites: Satellite[]): Monitor[] {
  const out: Monitor[] = [];
  const seen = new Set<string>();
  const maxRate = ch.max_sample_rate_hz ?? 2e6;
  for (const id of watchedSatellites(ch)) {
    const sat = satellites.find((s) => s.id === id);
    if (!sat) continue;
    for (const tp of sat.transponders) {
      const span = Math.min(SPAN_FACTOR * tp.bandwidth_hz, maxRate);
      const key = `${downlinkCenter(tp)}|${span}`;
      if (seen.has(key)) continue;
      seen.add(key);
      try {
        const [nfft, navg] = fitMonitorFrame(span);
        out.push({ id: `${sat.id}:${tp.id}`, center_hz: downlinkCenter(tp), span_hz: span, nfft, navg, frame_rate_hz: DEFAULT_RATE });
      } catch {
        /* the server reports this channel as invalid */
      }
    }
  }
  return out;
}

export function monitorsOf(ch: ReceiveChannel, satellites: Satellite[]): { monitors: Monitor[]; derived: boolean } {
  if (ch.monitors && ch.monitors.length) return { monitors: ch.monitors, derived: false };
  return { monitors: deriveMonitors(ch, satellites), derived: true };
}

export interface WindowInfo {
  key: string;
  site: Site;
  channel: ReceiveChannel;
  monitor: Monitor;
  derived: boolean;
}

export function windowKey(channelId: string, monitorId: string): string {
  return `${channelId}\u0000${monitorId}`;
}

export function allWindows(cfg: SystemConfig): WindowInfo[] {
  const out: WindowInfo[] = [];
  for (const site of cfg.sites) {
    for (const ch of site.channels) {
      const { monitors, derived } = monitorsOf(ch, cfg.satellites);
      for (const m of monitors) out.push({ key: windowKey(ch.id, m.id), site, channel: ch, monitor: m, derived });
    }
  }
  return out;
}

export function channelIndex(cfg: SystemConfig): Map<string, { site: Site; channel: ReceiveChannel }> {
  const m = new Map<string, { site: Site; channel: ReceiveChannel }>();
  for (const site of cfg.sites) for (const channel of site.channels) m.set(channel.id, { site, channel });
  return m;
}

export interface SmallDish {
  diameterM: number;
  freqHz: number;
  wavelengthM: number;
  diametersInWavelengths: number;
  /** boresight gain eta (pi D / lambda)^2, dBi */
  peakGainDbi: number;
  /** half-power beamwidth min(70 lambda / D, 120) degrees */
  hpbwDeg: number;
  satId: string;
}

/**
 * The caution case: a parabolic antenna under 3 wavelengths at the lowest frequency the
 * channel works at (the downlinks of the satellites it watches and its monitor centers),
 * the rule of SystemConfig.warnings.
 */
export function electricallySmall(ch: ReceiveChannel, satellites: Satellite[]): SmallDish | null {
  if (ch.antenna.type !== 'parabolic') return null;
  const ant = ch.antenna as ParabolicAntenna;
  let worst: SmallDish | null = null;
  const monitorFloor = Math.min(...(ch.monitors ?? []).map((m) => m.center_hz), Infinity);
  for (const id of watchedSatellites(ch)) {
    const sat = satellites.find((s) => s.id === id);
    if (!sat || !sat.transponders.length) continue;
    const f = Math.min(...sat.transponders.map(downlinkCenter), monitorFloor);
    const lambda = C_LIGHT / f;
    if (ant.diameter_m >= 3 * lambda) continue;
    const eta = ant.efficiency ?? 0.55;
    const g = 10 * Math.log10(eta * ((Math.PI * ant.diameter_m) / lambda) ** 2);
    const d: SmallDish = {
      diameterM: ant.diameter_m,
      freqHz: f,
      wavelengthM: lambda,
      diametersInWavelengths: ant.diameter_m / lambda,
      peakGainDbi: g,
      hpbwDeg: Math.min((70 * lambda) / ant.diameter_m, 120),
      satId: id,
    };
    if (!worst || d.diametersInWavelengths < worst.diametersInWavelengths) worst = d;
  }
  return worst;
}

/** Transponders whose uplink passband contains the frequency (MATH.md section 4). */
export function transpondersRelaying(cfg: SystemConfig, fUpHz: number): Array<{ sat: Satellite; tp: Transponder }> {
  const out: Array<{ sat: Satellite; tp: Transponder }> = [];
  for (const sat of cfg.satellites) {
    for (const tp of sat.transponders) {
      if (Math.abs(fUpHz - tp.uplink_center_hz) <= tp.bandwidth_hz / 2) out.push({ sat, tp });
    }
  }
  return out;
}
