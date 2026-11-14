// Ground positions: decimal degrees with hemisphere letters and the datum (WGS 84), and
// MGRS beside them. MGRS keeps its stated precision: 5 digits = 1 m.
import { forward as mgrsForward } from 'mgrs';
import { DASH } from './units';

export const DATUM = 'WGS 84';

/** Longitude wrapped to [-180, 180). */
export function wrapLon(lon: number): number {
  const w = ((((lon + 180) % 360) + 360) % 360) - 180;
  return Object.is(w, -0) ? 0 : w;
}

/** "20.3477° N" */
export function formatLat(lat: number | null | undefined, digits = 4): string {
  if (lat === null || lat === undefined || !Number.isFinite(lat)) return DASH;
  return `${Math.abs(lat).toFixed(digits)}° ${lat < 0 ? 'S' : 'N'}`;
}

/** "132.4220° E" (wrapped to [-180, 180)). */
export function formatLon(lon: number | null | undefined, digits = 4): string {
  if (lon === null || lon === undefined || !Number.isFinite(lon)) return DASH;
  const w = wrapLon(lon);
  return `${Math.abs(w).toFixed(digits)}° ${w < 0 ? 'W' : 'E'}`;
}

/** "20.3477° N 132.4220° E" */
export function formatLatLon(lat: number, lon: number, digits = 4): string {
  return `${formatLat(lat, digits)}  ${formatLon(lon, digits)}`;
}

/**
 * MGRS with the standard spacing, "52Q DE 12345 67890" (digits 5 = 1 m). Null outside the
 * UTM band (north of 84 N or south of 80 S, where UPS applies) or on invalid input.
 */
export function formatMgrs(lat: number, lon: number, digits = 5): string | null {
  if (!Number.isFinite(lat) || !Number.isFinite(lon) || lat > 84 || lat < -80) return null;
  let raw: string;
  try {
    raw = mgrsForward([wrapLon(lon), lat], digits);
  } catch {
    return null;
  }
  const m = raw.match(/^(\d{1,2}[C-HJ-NP-X])([A-HJ-NP-Z]{2})(\d*)$/);
  if (!m) return raw;
  const nums = m[3] ?? '';
  const half = nums.length / 2;
  return nums.length ? `${m[1]} ${m[2]} ${nums.slice(0, half)} ${nums.slice(half)}` : `${m[1]} ${m[2]}`;
}

const R_EARTH = 6371008.8;
const RAD = Math.PI / 180;

/** Great-circle distance in meters (spherical; for display only). */
export function distanceM(lat1: number, lon1: number, lat2: number, lon2: number): number {
  const p1 = lat1 * RAD;
  const p2 = lat2 * RAD;
  const dp = p2 - p1;
  const dl = (lon2 - lon1) * RAD;
  const a = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  return 2 * R_EARTH * Math.asin(Math.min(1, Math.sqrt(a)));
}

/**
 * Outline of an error ellipse as [lon, lat] points (closed), for drawing. Semi-axes in
 * meters, major-axis azimuth clockwise from true north. Local tangent-plane offsets are
 * converted with the spherical radius, which is exact enough for a drawn outline.
 */
export function ellipseRing(
  lat: number,
  lon: number,
  semiMajorM: number,
  semiMinorM: number,
  azimuthDeg: number,
  n = 96,
): Array<[number, number]> {
  const az = azimuthDeg * RAD;
  const cosLat = Math.max(Math.cos(lat * RAD), 1e-6);
  const ring: Array<[number, number]> = [];
  for (let i = 0; i <= n; i++) {
    const t = (2 * Math.PI * i) / n;
    const u = semiMajorM * Math.cos(t);
    const v = semiMinorM * Math.sin(t);
    const east = u * Math.sin(az) + v * Math.cos(az);
    const north = u * Math.cos(az) - v * Math.sin(az);
    ring.push([lon + east / (R_EARTH * cosLat) / RAD, lat + north / R_EARTH / RAD]);
  }
  return ring;
}
