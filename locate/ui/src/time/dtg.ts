// Military time formats (ACP 121 zone letters, DTG) computed from the live offset of an
// IANA zone, so daylight saving moves the letter. Zulu first, 24-hour only.
import { nsToDate } from './ns';

const MON = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'] as const;

// Intl.DateTimeFormat construction costs far more than formatting: one formatter per
// (kind, zone), reused (a 500-row measurement table formats 500 times per update).
const formatters = new Map<string, Intl.DateTimeFormat>();
function formatter(kind: string, timeZone: string, make: () => Intl.DateTimeFormat): Intl.DateTimeFormat {
  const key = `${kind}|${timeZone}`;
  let f = formatters.get(key);
  if (!f) {
    f = make();
    formatters.set(key, f);
  }
  return f;
}

/** Minutes east of UTC for an IANA zone at an instant. */
export function offsetMinutes(date: Date, timeZone = 'UTC'): number {
  const part = formatter('offset', timeZone, () => new Intl.DateTimeFormat('en-US', { timeZone, timeZoneName: 'longOffset' }))
    .formatToParts(date)
    .find((p) => p.type === 'timeZoneName');
  const m = part?.value.match(/GMT([+-])(\d{2}):?(\d{2})?/);
  if (!m) return 0;
  return (m[1] === '-' ? -1 : 1) * (Number(m[2]) * 60 + Number(m[3] ?? 0));
}

/** ACP 121 letter for a whole-hour offset; null for half-hour zones and beyond +/-12 h. */
export function zoneLetter(offsetMin: number): string | null {
  if (offsetMin % 60 !== 0) return null;
  const h = offsetMin / 60;
  if (h === 0) return 'Z';
  if (h >= 1 && h <= 9) return String.fromCharCode(64 + h); // A..I
  if (h >= 10 && h <= 12) return String.fromCharCode(65 + h); // K..M (J is local time)
  if (h <= -1 && h >= -12) return String.fromCharCode(77 - h); // N..Y
  return null;
}

/** "+05:30" style numeric offset (U+2212 minus sign for negative offsets). */
export function offsetLabel(offsetMin: number): string {
  const sign = offsetMin < 0 ? '−' : '+';
  const a = Math.abs(offsetMin);
  return `${sign}${String(Math.floor(a / 60)).padStart(2, '0')}:${String(a % 60).padStart(2, '0')}`;
}

interface Parts {
  yy: string;
  mon: string;
  dd: string;
  hh: string;
  mm: string;
  ss: string;
}

function parts(date: Date, timeZone: string): Parts {
  const p: Record<string, string> = {};
  const f = formatter(
    'parts',
    timeZone,
    () =>
      new Intl.DateTimeFormat('en-US', {
        timeZone,
        year: '2-digit',
        month: 'numeric',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hourCycle: 'h23',
      }),
  );
  for (const x of f.formatToParts(date)) {
    p[x.type] = x.value;
  }
  return {
    yy: p.year ?? '00',
    mon: MON[Number(p.month ?? '1') - 1] ?? 'JAN',
    dd: p.day ?? '01',
    hh: p.hour ?? '00',
    mm: p.minute ?? '00',
    ss: p.second ?? '00',
  };
}

/** DTG "DDHHMM<letter> MON YY"; zones without a letter fall back to Zulu. */
export function dtg(date: Date, timeZone = 'UTC'): string {
  let zone = timeZone;
  let letter = zoneLetter(offsetMinutes(date, zone));
  if (!letter) {
    letter = 'Z';
    zone = 'UTC';
  }
  const p = parts(date, zone);
  return `${p.dd}${p.hh}${p.mm}${letter} ${p.mon} ${p.yy}`;
}

/** DTG of a nanosecond timestamp string, Zulu. */
export function dtgNs(ns: string): string {
  return dtg(nsToDate(ns), 'UTC');
}

/** DTG with seconds, Zulu: "DDHHMM:SSZ MON YY" (for event times that change by the second). */
export function dtgSecondsNs(ns: string): string {
  const p = parts(nsToDate(ns), 'UTC');
  return `${p.dd}${p.hh}${p.mm}:${p.ss}Z ${p.mon} ${p.yy}`;
}

/** Zulu time of day "HH:MM:SSZ". */
export function zuluClock(date: Date): string {
  const p = parts(date, 'UTC');
  return `${p.hh}:${p.mm}:${p.ss}Z`;
}

/** Local time of day in a zone, "HHMM". */
export function localHhmm(date: Date, timeZone: string): string {
  const p = parts(date, timeZone);
  return `${p.hh}${p.mm}`;
}

/** Day marker against Zulu: "+1", "-1" or "" on the same calendar date. */
export function dayMarker(date: Date, timeZone: string): string {
  const ymd = (tz: string) =>
    formatter('ymd', tz, () => new Intl.DateTimeFormat('en-CA', { timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit' })).format(date);
  const d = Math.round((Date.parse(ymd(timeZone)) - Date.parse(ymd('UTC'))) / 864e5);
  return d > 0 ? `+${d}` : d < 0 ? `−${-d}` : '';
}

/** One entry of the clock strip: local HHMM, zone letter (or null) and numeric offset. */
export interface ZoneClock {
  hhmm: string;
  letter: string | null;
  offset: string;
  day: string;
}

export function zoneClock(date: Date, timeZone: string): ZoneClock {
  const off = offsetMinutes(date, timeZone);
  return { hhmm: localHhmm(date, timeZone), letter: zoneLetter(off), offset: offsetLabel(off), day: dayMarker(date, timeZone) };
}

/** Human duration for ages and lags: "0.4 s", "12 s", "4 min 05 s", "3 h 12 min", "2 d 4 h". */
export function formatAge(seconds: number): string {
  if (!Number.isFinite(seconds)) return '—';
  const s = Math.max(0, seconds);
  if (s < 10) return `${s.toFixed(1)} s`;
  if (s < 60) return `${Math.round(s)} s`;
  if (s < 3600) {
    const whole = Math.round(s);
    const m = Math.floor(whole / 60);
    if (m >= 60) return '1 h 00 min';
    return `${m} min ${String(whole - m * 60).padStart(2, '0')} s`;
  }
  if (s < 86400) {
    const h = Math.floor(s / 3600);
    return `${h} h ${String(Math.floor((s - h * 3600) / 60)).padStart(2, '0')} min`;
  }
  const d = Math.floor(s / 86400);
  return `${d} d ${Math.floor((s - d * 86400) / 3600)} h`;
}

/** ISO 8601 for file names: "2026-11-13T1800Z". */
export function isoFileStamp(date: Date): string {
  const iso = date.toISOString();
  return `${iso.slice(0, 10)}T${iso.slice(11, 13)}${iso.slice(14, 16)}Z`;
}
