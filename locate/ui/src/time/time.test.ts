import { describe, expect, it } from 'vitest';
import { dayMarker, dtg, dtgNs, dtgSecondsNs, formatAge, isoFileStamp, offsetLabel, offsetMinutes, zoneClock, zoneLetter, zuluClock } from './dtg';
import { isNsString, msToNs, nsAdd, nsCompare, nsDiff, nsDiffSeconds, nsMax, nsToBigInt, nsToDate, nsToIso, nsToMs } from './ns';
import { nearestZone, siteZone } from './zones';

describe('nanosecond strings', () => {
  const t = '1763056800123456789'; // 2025-11-13T18:00:00.123456789Z
  it('never loses digits that a Number would', () => {
    expect(Number(t)).not.toBe(1763056800123456789n); // why the strings exist
    expect(nsToBigInt(t)).toBe(1763056800123456789n);
    expect(nsAdd(t, 1)).toBe('1763056800123456790');
    expect(nsAdd(t, 1n)).toBe('1763056800123456790');
    expect(nsDiff('1763056800123456790', t)).toBe(1n);
  });
  it('converts to milliseconds and dates by BigInt division', () => {
    expect(nsToMs(t)).toBe(1763056800123);
    expect(nsToDate(t).toISOString()).toBe('2025-11-13T18:00:00.123Z');
    expect(msToNs(1763056800123)).toBe('1763056800123000000');
  });
  it('formats ISO 8601 with full precision for exports', () => {
    expect(nsToIso(t)).toBe('2025-11-13T18:00:00.123456789Z');
    expect(nsToIso(t, 3)).toBe('2025-11-13T18:00:00.123Z');
    expect(nsToIso(t, 0)).toBe('2025-11-13T18:00:00Z');
    expect(nsToIso('5')).toBe('1970-01-01T00:00:00.000000005Z');
  });
  it('compares and subtracts', () => {
    expect(nsCompare('2', '10')).toBe(-1);
    expect(nsCompare('10', '10')).toBe(0);
    expect(nsMax('10', '9')).toBe('10');
    expect(nsDiffSeconds('1763056805623456789', t)).toBeCloseTo(5.5, 9);
    expect(nsDiffSeconds(t, '1763056805623456789')).toBeCloseTo(-5.5, 9);
  });
  it('rejects anything that is not a decimal string', () => {
    expect(isNsString('123')).toBe(true);
    expect(isNsString('1.5e18')).toBe(false);
    expect(isNsString(123)).toBe(false);
    expect(() => nsToBigInt('-1')).toThrow();
    expect(() => nsToBigInt('12a')).toThrow();
  });
});

describe('zone letters and offsets (ACP 121)', () => {
  it('maps whole-hour offsets to letters, skipping J', () => {
    expect(zoneLetter(0)).toBe('Z');
    expect(zoneLetter(60)).toBe('A');
    expect(zoneLetter(9 * 60)).toBe('I');
    expect(zoneLetter(10 * 60)).toBe('K');
    expect(zoneLetter(12 * 60)).toBe('M');
    expect(zoneLetter(-60)).toBe('N');
    expect(zoneLetter(-10 * 60)).toBe('W');
    expect(zoneLetter(-12 * 60)).toBe('Y');
  });
  it('has no letter for half-hour zones or beyond twelve hours', () => {
    expect(zoneLetter(330)).toBeNull();
    expect(zoneLetter(14 * 60)).toBeNull();
  });
  it('writes numeric offsets with a true minus sign', () => {
    expect(offsetLabel(-600)).toBe('−10:00');
    expect(offsetLabel(330)).toBe('+05:30');
    expect(offsetLabel(0)).toBe('+00:00');
  });
  it('computes the letter from the live offset, so daylight saving moves it', () => {
    const jan = new Date('2026-01-15T12:00:00Z');
    const jul = new Date('2026-07-15T12:00:00Z');
    expect(zoneLetter(offsetMinutes(jan, 'America/New_York'))).toBe('R');
    expect(zoneLetter(offsetMinutes(jul, 'America/New_York'))).toBe('Q');
    expect(zoneLetter(offsetMinutes(jan, 'Pacific/Honolulu'))).toBe('W');
    expect(zoneLetter(offsetMinutes(jan, 'Asia/Tokyo'))).toBe('I');
    expect(zoneLetter(offsetMinutes(jan, 'Asia/Manila'))).toBe('H');
    expect(zoneLetter(offsetMinutes(jan, 'Australia/Sydney'))).toBe('L');
    expect(zoneLetter(offsetMinutes(jul, 'Australia/Sydney'))).toBe('K');
    expect(offsetMinutes(jan, 'Asia/Kolkata')).toBe(330);
  });
});

describe('DTG and the clock strip', () => {
  const d = new Date('2026-11-13T18:05:42Z');
  it('formats DDHHMMZ MON YY', () => {
    expect(dtg(d)).toBe('131805Z NOV 26');
    const ns = msToNs(Date.parse('2026-11-13T18:05:42Z'));
    expect(dtgNs(ns)).toBe('131805Z NOV 26');
    expect(dtgSecondsNs(ns)).toBe('131805:42Z NOV 26');
  });
  it('formats local DTGs with the zone letter and falls back to Zulu without one', () => {
    expect(dtg(d, 'Pacific/Honolulu')).toBe('130805W NOV 26');
    expect(dtg(d, 'Asia/Tokyo')).toBe('140305I NOV 26');
    expect(dtg(d, 'Asia/Kolkata')).toBe('131805Z NOV 26');
  });
  it('shows Zulu time of day and day markers', () => {
    expect(zuluClock(d)).toBe('18:05:42Z');
    expect(dayMarker(d, 'Asia/Tokyo')).toBe('+1');
    expect(dayMarker(new Date('2026-11-13T05:00:00Z'), 'Pacific/Honolulu')).toBe('−1');
    expect(dayMarker(d, 'Pacific/Honolulu')).toBe('');
  });
  it('builds a clock-strip entry with letter, offset and day', () => {
    expect(zoneClock(d, 'Asia/Seoul')).toEqual({ hhmm: '0305', letter: 'I', offset: '+09:00', day: '+1' });
    expect(zoneClock(d, 'Pacific/Honolulu')).toEqual({ hhmm: '0805', letter: 'W', offset: '−10:00', day: '' });
    expect(zoneClock(d, 'Asia/Kolkata').letter).toBeNull();
  });
  it('formats ages and file stamps', () => {
    expect(formatAge(0.42)).toBe('0.4 s');
    expect(formatAge(42)).toBe('42 s');
    expect(formatAge(245)).toBe('4 min 05 s');
    expect(formatAge(3 * 3600 + 720)).toBe('3 h 12 min');
    expect(formatAge(119.6)).toBe('2 min 00 s'); // never "1 min 60 s"
    expect(formatAge(3599.7)).toBe('1 h 00 min');
    expect(formatAge(Infinity)).toBe('—');
    expect(isoFileStamp(d)).toBe('2026-11-13T1805Z');
  });
});

describe('site zones', () => {
  it('assigns the nearest tz principal location within the country of the site', () => {
    expect(siteZone(21.307, -157.858, '840')).toBe('Pacific/Honolulu');
    expect(siteZone(19.707, -155.085, '840')).toBe('Pacific/Honolulu');
    expect(siteZone(35.681, 139.767, '392')).toBe('Asia/Tokyo');
    expect(siteZone(35.18, 129.075, '410')).toBe('Asia/Seoul');
    expect(siteZone(14.599, 120.984, '608')).toBe('Asia/Manila');
  });
  it('does not put Naha in Taipei once the country is known', () => {
    expect(nearestZone(26.212, 127.679)).toBe('Asia/Taipei');
    expect(siteZone(26.212, 127.679, '392')).toBe('Asia/Tokyo');
  });
  it('falls back to the nearest principal location at sea or for an unknown country', () => {
    expect(siteZone(20.3, 132.4, null)).toBe(nearestZone(20.3, 132.4));
    expect(siteZone(21.3, -157.9, '999')).toBe('Pacific/Honolulu');
  });
});
