// Site time zones. A site's configured IANA zone (Site.time_zone) is used when the browser
// knows it. Otherwise the site takes the IANA zone of the nearest tz principal location
// among the zones of the country it stands in (country from the Natural Earth borders the
// map already loads), or of the nearest principal location overall when the country is
// not known yet or has no zone. Example: Naha's nearest principal location is Taipei, but
// Naha is in Japan.
import { ZONES } from './zones.gen';

const RAD = Math.PI / 180;

function angularDistance(lat1: number, lon1: number, lat2: number, lon2: number): number {
  const p1 = lat1 * RAD;
  const p2 = lat2 * RAD;
  const dl = (lon2 - lon1) * RAD;
  const c = Math.sin(p1) * Math.sin(p2) + Math.cos(p1) * Math.cos(p2) * Math.cos(dl);
  return Math.acos(Math.min(1, Math.max(-1, c)));
}

/**
 * IANA zone for a point. countryId: ISO 3166-1 numeric code ("392" for Japan) of the
 * country containing the point, or null when unknown.
 */
export function siteZone(latDeg: number, lonDeg: number, countryId: string | null = null): string {
  let best = 'UTC';
  let bestD = Infinity;
  let bestInCountry = 'UTC';
  let bestInCountryD = Infinity;
  for (const [tz, lat, lon, countries] of ZONES) {
    const d = angularDistance(latDeg, lonDeg, lat, lon);
    if (d < bestD) {
      bestD = d;
      best = tz;
    }
    if (countryId && countries.includes(countryId) && d < bestInCountryD) {
      bestInCountryD = d;
      bestInCountry = tz;
    }
  }
  return bestInCountryD < Infinity ? bestInCountry : best;
}

/** Zone of the nearest principal location, ignoring borders. */
export function nearestZone(latDeg: number, lonDeg: number): string {
  return siteZone(latDeg, lonDeg, null);
}

/** Whether the browser's time zone data knows an IANA zone name. */
export function knownZone(tz: string | null | undefined): tz is string {
  if (!tz) return false;
  try {
    new Intl.DateTimeFormat('en-US', { timeZone: tz });
    return true;
  } catch {
    return false;
  }
}
