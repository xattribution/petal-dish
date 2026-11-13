// Nanosecond timestamps arrive as decimal strings (PROTOCOL.md section B). They stay
// strings or BigInt; only millisecond values derived by BigInt division ever become
// Numbers (1.7e12 ms is well inside the 2^53 range, 1.7e18 ns is not).

const NS_PER_MS = 1_000_000n;
const NS_PER_S = 1_000_000_000n;
const DECIMAL = /^\d{1,19}$/;

/** True for a decimal nanosecond string such as "1763056800000000000". */
export function isNsString(value: unknown): value is string {
  return typeof value === 'string' && DECIMAL.test(value);
}

/** Parse a decimal nanosecond string to BigInt; throws on anything else. */
export function nsToBigInt(ns: string): bigint {
  if (!isNsString(ns)) throw new Error(`not a nanosecond timestamp: ${JSON.stringify(ns)}`);
  return BigInt(ns);
}

/** Milliseconds since the epoch (floor), as a Number, via BigInt division. */
export function nsToMs(ns: string): number {
  return Number(nsToBigInt(ns) / NS_PER_MS);
}

/** A Date for a nanosecond timestamp (millisecond resolution). */
export function nsToDate(ns: string): Date {
  return new Date(nsToMs(ns));
}

/** Nanoseconds for an epoch-millisecond Number (integer part only). */
export function msToNs(ms: number): string {
  return (BigInt(Math.trunc(ms)) * NS_PER_MS).toString();
}

/** a - b in nanoseconds, as a decimal string (may be negative). */
export function nsDiff(a: string, b: string): bigint {
  return nsToBigInt(a) - nsToBigInt(b);
}

/** a - b in seconds as a Number (for durations; exact to the microsecond up to ~285 years). */
export function nsDiffSeconds(a: string, b: string): number {
  const d = nsDiff(a, b);
  const whole = d / NS_PER_S;
  const frac = d % NS_PER_S;
  return Number(whole) + Number(frac) / 1e9;
}

/** Add whole nanoseconds (BigInt or integer Number) to a timestamp string. */
export function nsAdd(ns: string, delta: bigint | number): string {
  const d = typeof delta === 'bigint' ? delta : BigInt(Math.trunc(delta));
  return (nsToBigInt(ns) + d).toString();
}

/** Compare two timestamp strings: negative, zero or positive. */
export function nsCompare(a: string, b: string): number {
  const d = nsDiff(a, b);
  return d < 0n ? -1 : d > 0n ? 1 : 0;
}

/** The later of two timestamps. */
export function nsMax(a: string, b: string): string {
  return nsCompare(a, b) >= 0 ? a : b;
}

/**
 * ISO 8601 UTC with nanoseconds, for exports and machines: "2026-11-13T18:00:00.123456789Z".
 * Built from BigInt parts so no digit is lost.
 */
export function nsToIso(ns: string, digits: 0 | 3 | 6 | 9 = 9): string {
  const v = nsToBigInt(ns);
  const ms = Number(v / NS_PER_MS);
  const base = new Date(Math.floor(ms / 1000) * 1000).toISOString().slice(0, 19);
  if (digits === 0) return `${base}Z`;
  const frac = (v % NS_PER_S).toString().padStart(9, '0').slice(0, digits);
  return `${base}.${frac}Z`;
}
