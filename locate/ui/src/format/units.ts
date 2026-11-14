// Number formatting in the units operators read: MHz with Hz precision, kHz for widths,
// microseconds for time differences, Hz/mHz for frequency differences, km for distances.
// Negative numbers use the true minus sign (U+2212); digit groups after the decimal point
// are separated by a thin space so 301.005 000 reads to the hertz.

export const MINUS = '−';
const THIN = ' ';
export const DASH = '—';

function signed(text: string): string {
  return text.startsWith('-') ? MINUS + text.slice(1) : text;
}

/** Fixed decimals with the true minus sign; em dash for null or non-finite. */
export function fixed(value: number | null | undefined, digits: number): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return DASH;
  const t = value.toFixed(digits);
  return signed(/^-0\.?0*$/.test(t) ? t.slice(1) : t);
}

/** Group six or more fractional digits in threes: "301.005000" becomes "301.005 000" (shorter fractions stay whole). */
function groupFraction(text: string): string {
  const [int, frac] = text.split('.');
  if (!frac || frac.length < 6) return text;
  return `${int}.${frac.replace(/(\d{3})(?=\d)/g, `$1${THIN}`)}`;
}

/** Frequency in MHz: 301005000 becomes "301.005 000" (6 decimals = 1 Hz). */
export function mhz(hz: number | null | undefined, digits = 6): string {
  if (hz === null || hz === undefined || !Number.isFinite(hz)) return DASH;
  return signed(groupFraction((hz / 1e6).toFixed(digits)));
}

/** Bandwidth or span in kHz: 25000 becomes "25.0". */
export function khz(hz: number | null | undefined, digits = 1): string {
  return fixed(hz === null || hz === undefined ? null : hz / 1e3, digits);
}

/** Frequency or width with an automatic unit: "8.00 kHz", "260.005 000 MHz", "12 Hz". */
export function freqAuto(hz: number | null | undefined): string {
  if (hz === null || hz === undefined || !Number.isFinite(hz)) return DASH;
  const a = Math.abs(hz);
  if (a >= 1e6) return `${mhz(hz)} MHz`;
  if (a >= 1e3) return `${fixed(hz / 1e3, a >= 1e5 ? 1 : 2)} kHz`;
  return `${fixed(hz, a >= 100 ? 0 : 1)} Hz`;
}

/** Time difference in microseconds: 7.8635e-3 s becomes "7 863.548" (ns resolution). */
export function micros(seconds: number | null | undefined, digits = 3): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return DASH;
  return groupInt(fixed(seconds * 1e6, digits));
}

/** Frequency difference in Hz with mHz resolution: "−12.904". */
export function hz3(hz: number | null | undefined): string {
  return groupInt(fixed(hz, 3));
}

/** Frequency difference in mHz: 0.00157 Hz becomes "1.57". */
export function millihertz(hz: number | null | undefined, digits = 2): string {
  return fixed(hz === null || hz === undefined ? null : hz * 1e3, digits);
}

/** Thousands separator (thin space) on the integer part of an already formatted number. */
export function groupInt(text: string): string {
  const m = text.match(/^([−-]?)(\d+)(.*)$/);
  if (!m) return text;
  const int = m[2] ?? '';
  if (int.length <= 4) return text;
  return `${m[1]}${int.replace(/\B(?=(\d{3})+(?!\d))/g, THIN)}${m[3]}`;
}

/** Distance from meters in km with precision that follows size: "0.60", "4.18", "10.6", "1 780". */
export function km(meters: number | null | undefined): string {
  if (meters === null || meters === undefined || !Number.isFinite(meters)) return DASH;
  const k = meters / 1000;
  const a = Math.abs(k);
  const digits = a < 10 ? 2 : a < 100 ? 1 : 0;
  return groupInt(fixed(k, digits));
}

/** Decibels to one decimal: "17.8". */
export function db(value: number | null | undefined, digits = 1): string {
  return fixed(value, digits);
}

/** Degrees: "52.4". */
export function deg(value: number | null | undefined, digits = 1): string {
  return fixed(value, digits);
}

/** A fraction 0..1 as a percentage: 0.91 becomes "91 %". */
export function percent(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return DASH;
  return `${Math.round(value * 100)} %`;
}

/** Nanoseconds to a readable clock offset: 1650 becomes "1.65 µs", 14 becomes "14 ns". */
export function clockOffset(ns: number | null | undefined): string {
  if (ns === null || ns === undefined || !Number.isFinite(ns)) return DASH;
  const a = Math.abs(ns);
  if (a >= 1e6) return `${fixed(ns / 1e6, 2)} ms`;
  if (a >= 1e3) return `${fixed(ns / 1e3, 2)} µs`;
  return `${fixed(ns, 0)} ns`;
}

/**
 * Parse a decimal MHz string to integer Hz without floating-point error:
 * "301.0125" becomes 301012500. Returns null for anything that is not a plain decimal.
 */
export function parseMhzToHz(text: string): number | null {
  const t = text.trim().replace(/[\s  ]/g, '').replace('−', '-');
  const m = t.match(/^(-?)(\d*)(?:\.(\d*))?$/);
  if (!m || (!m[2] && !m[3])) return null;
  const frac = (m[3] ?? '').padEnd(6, '0');
  const hzInt = BigInt(m[2] || '0') * 1_000_000n + BigInt(frac.slice(0, 6) || '0');
  const rest = frac.slice(6);
  let value = Number(hzInt);
  if (rest.length > 0) value += Number(`0.${rest}`);
  return m[1] === '-' ? -value : value;
}

/** Hz to the MHz text an input shows (trailing zeros trimmed, at least 3 decimals). */
export function hzToMhzInput(hz: number): string {
  const t = (hz / 1e6).toFixed(9).replace(/0+$/, '');
  const [i, f = ''] = t.split('.');
  return `${i}.${f.padEnd(3, '0')}`;
}

/** "1 job", "2 jobs". */
export function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? '' : 's'}`;
}
