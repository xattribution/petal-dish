// Map geometry: the error ellipse keeps its true azimuth and size on screen (Mercator is
// conformal), and rings, ellipses and lines of position across the antimeridian stay in
// one piece.
import { geoMercator, geoPath } from 'd3-geo';
import { describe, expect, it } from 'vitest';
import { ellipseRing } from '../format/geo';
import { invMercY, mercY, niceKm } from './MapView';

const RAD = Math.PI / 180;

function projection(lon0: number, lat0: number, k: number) {
  return geoMercator()
    .rotate([-lon0, 0])
    .scale(k)
    .translate([500, 500 + mercY(lat0) * k]);
}

describe('mercator helpers', () => {
  it('inverts', () => {
    for (const lat of [-60, -20, 0, 20.35, 49.5, 80]) expect(invMercY(mercY(lat))).toBeCloseTo(lat, 9);
  });
  it('picks 1-2-5 scale bars', () => {
    expect(niceKm(120)).toBe(100);
    expect(niceKm(43)).toBe(20);
    expect(niceKm(7.9)).toBe(5);
    expect(niceKm(0.31)).toBeCloseTo(0.2, 12);
  });
});

describe('error ellipse on the map', () => {
  for (const [lat, az] of [
    [20.35, 3],
    [49.54, 10],
    [49.54, 45],
    [-35, 120],
  ] as const) {
    it(`draws the major axis at ${az}° true and the right length at ${lat}°`, () => {
      const lon = 138.4;
      const a = 28_400;
      const b = 670;
      const k = 2_000_000; // about 3 m per pixel at the equator
      const proj = projection(lon, lat, k);
      const ring = ellipseRing(lat, lon, a, b, az, 360);
      const c = proj([lon, lat]) as [number, number];
      // farthest projected point from the center = end of the major axis
      let best = { d: 0, x: 0, y: 0 };
      for (const pt of ring) {
        const [x, y] = proj(pt) as [number, number];
        const d = Math.hypot(x - c[0], y - c[1]);
        if (d > best.d) best = { d, x: x - c[0], y: y - c[1] };
      }
      // screen bearing clockwise from up, folded to 0..180 like EllipseOut.azimuth_deg
      const bearing = (((Math.atan2(best.x, -best.y) / RAD) % 180) + 180) % 180;
      expect(Math.abs(bearing - az)).toBeLessThan(0.6);
      // on-screen length matches the Mercator scale at that latitude
      const mPerPx = (6371008.8 * Math.cos(lat * RAD)) / k;
      expect(best.d * mPerPx).toBeGreaterThan(a * 0.97);
      expect(best.d * mPerPx).toBeLessThan(a * 1.03);
    });
  }

  it('keeps an ellipse on the antimeridian in one piece', () => {
    const proj = projection(180, 10, 400_000);
    const ring = ellipseRing(10, 179.98, 20_000, 5_000, 80);
    expect(ring.some(([l]) => l > 180)).toBe(true);
    const [[x0], [x1]] = geoPath(proj).bounds({ type: 'Polygon', coordinates: [ring] }); // clockwise, as d3 wants
    const widthPx = x1 - x0;
    const expected = (2 * 20_000 * 400_000) / (6371008.8 * Math.cos(10 * RAD)); // about 2 semi-major axes
    expect(widthPx).toBeLessThan(expected * 1.1);
    expect(widthPx).toBeGreaterThan(expected * 0.8);
  });

  it('draws an unwrapped line of position (longitudes past 180) as one short line', () => {
    const proj = projection(-175, 20, 3000);
    const line = { type: 'MultiLineString' as const, coordinates: [[[178, 18], [181, 20], [184, 22]]] };
    const [[x0], [x1]] = geoPath(proj).bounds(line);
    expect(x1 - x0).toBeLessThan(6 * RAD * 3000 * 1.05);
  });
});
