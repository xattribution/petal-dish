import { describe, expect, it } from 'vitest';
import { distanceM, ellipseRing, formatLat, formatLatLon, formatLon, formatMgrs, wrapLon } from './geo';
import { clockOffset, db, fixed, freqAuto, groupInt, hz3, hzToMhzInput, khz, km, mhz, micros, millihertz, parseMhzToHz, percent } from './units';

const T = ' ';
const M = '−';

describe('units', () => {
  it('formats MHz to the hertz with grouped decimals', () => {
    expect(mhz(301005000)).toBe(`301.005${T}000`);
    expect(mhz(260502500)).toBe(`260.502${T}500`);
    expect(mhz(301.0125e6, 4)).toBe(`301.012${T}5`);
    expect(mhz(null)).toBe('—');
  });
  it('formats widths and automatic units', () => {
    expect(khz(25000)).toBe('25.0');
    expect(freqAuto(8000)).toBe('8.00 kHz');
    expect(freqAuto(12)).toBe('12.0 Hz');
    expect(freqAuto(301005000)).toBe(`301.005${T}000 MHz`);
  });
  it('formats time differences in microseconds and frequency differences in Hz and mHz', () => {
    expect(micros(7.863548048e-3)).toBe('7863.548');
    expect(micros(17.863548048e-3)).toBe(`17${T}863.548`);
    expect(micros(-3.2231786e-7)).toBe(`${M}0.322`);
    expect(micros(4.58e-7)).toBe('0.458');
    expect(hz3(-12.9044697)).toBe(`${M}12.904`);
    expect(hz3(0.0276)).toBe('0.028');
    expect(millihertz(0.0015728)).toBe('1.57');
  });
  it('never prints negative zero and uses the true minus sign', () => {
    expect(fixed(-0.0001, 2)).toBe('0.00');
    expect(fixed(-1.5, 1)).toBe(`${M}1.5`);
    expect(db(-3.25)).toBe(`${M}3.3`);
  });
  it('formats distances in km with precision that follows size', () => {
    expect(km(600)).toBe('0.60');
    expect(km(4178)).toBe('4.18');
    expect(km(10571)).toBe('10.6');
    expect(km(1780000)).toBe('1780');
    expect(km(17800000)).toBe(`17${T}800`);
    expect(km(undefined)).toBe('—');
  });
  it('groups thousands only past four digits', () => {
    expect(groupInt('1234.5')).toBe('1234.5');
    expect(groupInt('12345.5')).toBe(`12${T}345.5`);
    expect(groupInt(`${M}123456`)).toBe(`${M}123${T}456`);
  });
  it('formats clock offsets, percentages', () => {
    expect(clockOffset(1650)).toBe('1.65 µs');
    expect(clockOffset(-9)).toBe(`${M}9 ns`);
    expect(clockOffset(2.5e6)).toBe('2.50 ms');
    expect(percent(0.91)).toBe('91 %');
  });
  it('parses decimal MHz to exact hertz', () => {
    expect(parseMhzToHz('301.0125')).toBe(301012500);
    expect(parseMhzToHz(' 300.9925 ')).toBe(300992500);
    expect(parseMhzToHz('-41')).toBe(-41000000);
    expect(parseMhzToHz('301.0000005')).toBeCloseTo(301000000.5, 6);
    expect(parseMhzToHz('abc')).toBeNull();
    expect(parseMhzToHz('')).toBeNull();
    expect(hzToMhzInput(301012500)).toBe('301.0125');
    expect(hzToMhzInput(-41e6)).toBe('-41.000');
  });
});

describe('positions', () => {
  it('writes decimal degrees with hemisphere letters', () => {
    expect(formatLat(20.34774)).toBe('20.3477° N');
    expect(formatLat(-7.9)).toBe('7.9000° S');
    expect(formatLon(132.422)).toBe('132.4220° E');
    expect(formatLon(-157.858)).toBe('157.8580° W');
    expect(formatLon(202.142)).toBe('157.8580° W');
    expect(formatLatLon(21.307, -157.858, 3)).toBe('21.307° N  157.858° W');
  });
  it('wraps longitudes to [-180, 180)', () => {
    expect(wrapLon(180)).toBe(-180);
    expect(wrapLon(240)).toBe(-120);
    expect(wrapLon(-181)).toBe(179);
    expect(wrapLon(-0)).toBe(0);
  });
  it('gives MGRS with standard spacing and 1 m digits', () => {
    expect(formatMgrs(21.307, -157.858)).toMatch(/^4Q FJ \d{5} \d{5}$/);
    expect(formatMgrs(35.681, 139.767)).toMatch(/^54S UE \d{5} \d{5}$/);
    expect(formatMgrs(20.3477, 132.422)).toMatch(/^53Q /); // UTM zone 53 is 132 E to 138 E
    expect(formatMgrs(20.3477, 131.9)).toMatch(/^52Q /);
    expect(formatMgrs(35.681, 139.767, 2)).toMatch(/^54S UE \d{2} \d{2}$/);
  });
  it('declines MGRS in the polar UPS zones', () => {
    expect(formatMgrs(85, 10)).toBeNull();
    expect(formatMgrs(-81, 10)).toBeNull();
  });
  it('measures great-circle distance', () => {
    expect(distanceM(35.18, 129.075, 37.566, 126.978) / 1000).toBeCloseTo(325, -1);
  });
  it('draws ellipses with the major axis along the azimuth', () => {
    const ring = ellipseRing(0, 0, 10000, 1000, 0, 4);
    expect(ring).toHaveLength(5);
    expect(ring[0]![1]).toBeCloseTo(10000 / 6371008.8 * 180 / Math.PI, 6); // north
    expect(ring[0]![0]).toBeCloseTo(0, 9);
    const east = ellipseRing(0, 0, 10000, 1000, 90, 4);
    expect(east[0]![0]).toBeGreaterThan(0.08);
    expect(Math.abs(east[0]![1])).toBeLessThan(1e-9);
  });
});
