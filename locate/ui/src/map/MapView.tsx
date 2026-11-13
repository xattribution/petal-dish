// World map on canvas: Mercator, centered on the region of interest and rotated so the
// antimeridian is just another meridian (d3-geo cuts geometry at the view's edge, not at
// 180 degrees). Mercator is conformal, so an error ellipse keeps its true shape and its
// major-axis azimuth reads correctly off the map at any latitude, and lines of position
// cross at their true angle. Pan by dragging or arrow keys, zoom by wheel or +/-; panning
// in longitude wraps around the globe. Layer toggles and legend sit in a strip under the
// map so they never cover data; the scale bar is drawn for the center latitude.
import { geoArea, geoGraticule, geoMercator, geoPath, type GeoProjection } from 'd3-geo';
import type { Feature, Geometry } from 'geojson';
import { useEffect, useRef, useState, type ReactNode } from 'react';
import type { AccuracyGrid, Footprint, LopCollection, LopFeature, Region, Subpoint } from '../api/types.gen';
import { ellipseRing, formatLat, formatLon, formatMgrs, wrapLon } from '../format/geo';
import { km } from '../format/units';
import { color, onThemeChange } from '../styles/theme';
import { loadWorld, worldIfLoaded, type World } from './world';

export type LayerId = 'sites' | 'satellites' | 'footprints' | 'events' | 'lops' | 'accuracy' | 'truth';

export interface MapSite {
  id: string;
  name: string;
  lat: number;
  lon: number;
}
export interface MapSolution {
  id: string;
  eventId: string;
  lat: number;
  lon: number;
  semiMajorM: number;
  semiMinorM: number;
  azimuthDeg: number;
  role: 'fix' | 'mirror';
  selected: boolean;
  label: string;
}
export interface MapTruth {
  id: string;
  lat: number;
  lon: number;
  label: string;
  selected?: boolean;
  enabled?: boolean;
}
export interface MapHeat {
  grid: AccuracyGrid;
  field: 'semi_major_95_m' | 'cep50_m';
  letters: boolean;
}
export interface MapData {
  sites?: MapSite[];
  subpoints?: Subpoint[];
  footprints?: Footprint[];
  solutions?: MapSolution[];
  lops?: LopCollection | null;
  heat?: MapHeat | null;
  truth?: MapTruth[];
}
/** What to show: everything between latMin..latMax, lonMin..lonMax (lonMax may exceed 180). */
export interface MapFocus {
  latMin: number;
  latMax: number;
  lonMin: number;
  lonMax: number;
  key: string;
}

export interface MapProps {
  data: MapData;
  /** layers offered in the legend and whether each is on */
  layers: Partial<Record<LayerId, boolean>>;
  onLayers?: (next: Partial<Record<LayerId, boolean>>) => void;
  region?: Region | null;
  focus?: MapFocus | null;
  onMapClick?: (lat: number, lon: number) => void;
  onPick?: (kind: 'solution' | 'truth' | 'site', id: string) => void;
  placing?: boolean;
  label: string;
  truthLabel?: string;
  /** confidence of the drawn ellipses, 0..1 (legend text) */
  ellipseConfidence?: number;
  children?: ReactNode;
}

const RAD = Math.PI / 180;
const R_EARTH_M = 6371008.8;
const MAX_LAT = 85;
export const HEAT_EDGES_KM = [1, 3, 10, 30, 100, 300];
export const DOMINANT_LETTER: Record<string, string> = {
  noise: 'N',
  ephemeris_position: 'P',
  ephemeris_velocity: 'V',
  clock: 'C',
  oscillator: 'O',
  tdoa_bias: 'B',
  reference: 'R',
};
export const DOMINANT_WORD: Record<string, string> = {
  noise: 'measurement noise',
  ephemeris_position: 'ephemeris position',
  ephemeris_velocity: 'ephemeris velocity',
  clock: 'site clocks',
  oscillator: 'oscillators',
  tdoa_bias: 'TDOA bias',
  reference: 'reference measurement',
};

function layerLabel(id: LayerId, p: MapProps): string {
  switch (id) {
    case 'sites':
      return 'Sites';
    case 'satellites':
      return 'Sub-satellite points';
    case 'footprints':
      return 'Footprints';
    case 'events':
      return `Fixes, ${Math.round((p.ellipseConfidence ?? 0.95) * 100)} % ellipses`;
    case 'lops':
      return 'Lines of position';
    case 'accuracy':
      return 'Predicted error';
    case 'truth':
      return p.truthLabel ?? 'Simulation truth';
  }
}

export function heatClass(meters: number): number {
  const v = meters / 1000;
  let i = 0;
  while (i < HEAT_EDGES_KM.length && v >= (HEAT_EDGES_KM[i] ?? Infinity)) i++;
  return i;
}

/** Mercator ordinate (radians of "isometric latitude") of a latitude in degrees. */
export function mercY(latDeg: number): number {
  const p = Math.max(-MAX_LAT, Math.min(MAX_LAT, latDeg)) * RAD;
  return Math.log(Math.tan(Math.PI / 4 + p / 2));
}
/** Inverse of mercY, degrees. */
export function invMercY(y: number): number {
  return (2 * Math.atan(Math.exp(y)) - Math.PI / 2) / RAD;
}

interface ViewState {
  lon0: number;
  lat0: number;
  k: number;
  w: number;
  h: number;
}

function regionBounds(r: Region | null | undefined): { lonMin: number; lonMax: number; latMin: number; latMax: number } {
  if (!r) return { lonMin: 60, lonMax: 240, latMin: -60, latMax: 60 };
  const full = r.lon_min === -180 && r.lon_max === 180;
  const lonMin = r.lon_min ?? -180;
  const lonMax = r.lon_max ?? 180;
  const span = full ? 360 : (((lonMax - lonMin) % 360) + 360) % 360 || 360;
  return { lonMin, lonMax: lonMin + span, latMin: r.lat_min ?? -70, latMax: r.lat_max ?? 70 };
}

function makeProjection(v: ViewState): GeoProjection {
  return geoMercator()
    .rotate([-v.lon0, 0])
    .scale(v.k)
    .translate([v.w / 2, v.h / 2 + mercY(v.lat0) * v.k])
    .precision(0.4);
}

/** d3 wants clockwise exterior rings; GeoJSON (RFC 7946) gives counter-clockwise. Fix up polygons. */
function d3Winding<T extends Feature<Geometry> | LopFeature>(f: T): T {
  const g = f.geometry as { type?: string; coordinates?: unknown } | null;
  if (!g || (g.type !== 'Polygon' && g.type !== 'MultiPolygon')) return f;
  if (geoArea(f as Feature<Geometry>) <= 2 * Math.PI) return f;
  const rev = (rings: number[][][]) => rings.map((r) => [...r].reverse());
  const geometry =
    g.type === 'Polygon'
      ? { type: 'Polygon' as const, coordinates: rev(g.coordinates as number[][][]) }
      : { type: 'MultiPolygon' as const, coordinates: (g.coordinates as number[][][][]).map(rev) };
  return { ...f, geometry } as T;
}

function ringFeature(ring: Array<[number, number]> | number[][]): Feature<Geometry> {
  return d3Winding({ type: 'Feature', properties: {}, geometry: { type: 'Polygon', coordinates: [ring as number[][]] } });
}

/** A "nice" scale-bar length (1, 2 or 5 times a power of ten) not longer than maxKm. */
export function niceKm(maxKm: number): number {
  if (!(maxKm > 0)) return 0;
  const p = 10 ** Math.floor(Math.log10(maxKm));
  for (const m of [5, 2, 1]) if (m * p <= maxKm) return m * p;
  return p;
}

interface Box {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

/** Greedy label placement: the first of four positions around the mark that overlaps nothing drawn so far. */
class Labeler {
  private boxes: Box[] = [];
  constructor(private ctx: CanvasRenderingContext2D) {}
  /** Reserve the area of a mark so labels avoid it. */
  mark(x: number, y: number, r: number): void {
    this.boxes.push({ x0: x - r, y0: y - r, x1: x + r, y1: y + r });
  }
  markBox(b: Box): void {
    this.boxes.push(b);
  }
  place(text: string, x: number, y: number, r: number, force: boolean): void {
    const ctx = this.ctx;
    const w = ctx.measureText(text).width;
    const h = 13;
    const tries: Array<[number, number]> = [
      [x + r + 3, y - r - 1],
      [x + r + 3, y + r + 1],
      [x - r - 3 - w, y - r - 1],
      [x - r - 3 - w, y + r + 1],
    ];
    const free = (b: Box) => !this.boxes.some((o) => b.x0 < o.x1 && b.x1 > o.x0 && b.y0 < o.y1 && b.y1 > o.y0);
    let pick: [number, number] | null = null;
    for (const [lx, ly] of tries) {
      if (free({ x0: lx - 1, y0: ly - h / 2, x1: lx + w + 1, y1: ly + h / 2 })) {
        pick = [lx, ly];
        break;
      }
    }
    if (!pick) {
      if (!force) return;
      pick = tries[0] ?? [x, y];
    }
    const [lx, ly] = pick;
    this.boxes.push({ x0: lx - 1, y0: ly - h / 2, x1: lx + w + 1, y1: ly + h / 2 });
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    ctx.lineWidth = 3;
    ctx.strokeStyle = color('--paper');
    ctx.strokeText(text, lx, ly);
    ctx.fillText(text, lx, ly);
  }
}

export function MapView(props: MapProps) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const readoutRef = useRef<HTMLSpanElement>(null);
  const propsRef = useRef(props);
  propsRef.current = props;
  const [world, setWorld] = useState<World | null>(worldIfLoaded());
  const [worldError, setWorldError] = useState<string | null>(null);
  const viewRef = useRef<ViewState | null>(null);
  const interactingUntil = useRef(0);
  const frame = useRef(0);
  const footprintFeatures = useRef<WeakMap<Footprint[], Feature<Geometry>[]>>(new WeakMap());
  const lopFeatures = useRef<WeakMap<LopCollection, LopFeature[]>>(new WeakMap());

  useEffect(() => {
    if (!world) loadWorld().then(setWorld, (e: Error) => setWorldError(e.message));
  }, [world]);

  const fitBounds = (b: { lonMin: number; lonMax: number; latMin: number; latMax: number }, margin: number) => {
    const v = viewRef.current;
    if (!v) return;
    const spanLon = Math.max(0.02, b.lonMax - b.lonMin);
    const yMin = mercY(b.latMin);
    const yMax = mercY(b.latMax);
    v.lon0 = wrapLon(b.lonMin + spanLon / 2);
    v.lat0 = invMercY((yMin + yMax) / 2);
    v.k = Math.max(80, Math.min(400000, Math.min(v.w / (spanLon * RAD), v.h / Math.max(1e-4, yMax - yMin)) * margin));
    schedule();
  };

  const resetView = () => fitBounds(regionBounds(propsRef.current.region), 0.98);

  const schedule = () => {
    if (frame.current) return;
    frame.current = requestAnimationFrame(() => {
      frame.current = 0;
      draw();
    });
  };

  // ---------------------------------------------------------------- drawing
  const draw = () => {
    const canvas = canvasRef.current;
    const v = viewRef.current;
    if (!canvas || !v || v.w === 0) return;
    const p = propsRef.current;
    const dpr = window.devicePixelRatio || 1;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const proj = makeProjection(v);
    const path = geoPath(proj, ctx);
    const on = (id: LayerId) => p.layers[id] === true;

    ctx.fillStyle = color('--surface-2');
    ctx.fillRect(0, 0, v.w, v.h);
    ctx.beginPath();
    path({ type: 'Sphere' });
    ctx.fillStyle = color('--water');
    ctx.fill();

    // graticule, coarser when zoomed out
    const degPerPx = 1 / (v.k * RAD);
    const step = [0.1, 0.25, 0.5, 1, 2, 5, 10, 30].find((s) => s / degPerPx >= 70) ?? 30;
    ctx.beginPath();
    path(geoGraticule().step([step, step]).extentMajor([[-180, -MAX_LAT], [180, MAX_LAT]])());
    ctx.strokeStyle = color('--graticule');
    ctx.lineWidth = 1;
    ctx.stroke();

    // land and borders
    const moving = performance.now() < interactingUntil.current;
    if (world) {
      ctx.beginPath();
      path(moving ? world.land110 : world.land50);
      ctx.fillStyle = color('--land');
      ctx.fill();
      ctx.strokeStyle = color('--coast');
      ctx.lineWidth = 0.7;
      ctx.stroke();
      if (!moving) {
        ctx.beginPath();
        path(world.borders50);
        ctx.strokeStyle = color('--border');
        ctx.lineWidth = 0.6;
        ctx.stroke();
      }
    }

    const cx = v.w / 2;
    const cy = v.h / 2;
    const y0Merc = mercY(v.lat0);
    const xOf = (lon: number) => cx + (((((lon - v.lon0 + 180) % 360) + 360) % 360) - 180) * RAD * v.k;
    const yOf = (lat: number) => cy - (mercY(lat) - y0Merc) * v.k;

    // accuracy heat map (cells are lat/lon rectangles; Mercator stretches them north-south)
    if (on('accuracy') && p.data.heat) {
      const { grid, field, letters } = p.data.heat;
      const lats = grid.lat_deg;
      const lons = grid.lon_deg;
      const dLat = lats.length > 1 ? Math.abs((lats[1] ?? 0) - (lats[0] ?? 0)) : 1;
      const dLon = lons.length > 1 ? (lons[1] ?? 0) - (lons[0] ?? 0) : 1;
      const values = grid[field];
      const fills = [0, 1, 2, 3, 4, 5, 6].map((i) => color(`--heat-${i}`));
      ctx.globalAlpha = 0.8;
      const cellW = dLon * RAD * v.k;
      for (let i = 0; i < lats.length; i++) {
        const row = values[i];
        if (!row) continue;
        const lat = lats[i] ?? 0;
        const yTop = yOf(lat + dLat / 2);
        const yBot = yOf(lat - dLat / 2);
        if (yTop > v.h || yBot < 0) continue;
        for (let j = 0; j < lons.length; j++) {
          const val = row[j];
          if (val === null || val === undefined) continue;
          const lon = lons[j] ?? 0;
          const x0 = xOf(lon) - cellW / 2;
          if (x0 > v.w || x0 + cellW < 0) continue;
          // whole-pixel edges so neighbors abut without overlapping (no seams under the alpha)
          const xa = Math.round(x0);
          const ya = Math.round(yTop);
          ctx.fillStyle = fills[heatClass(val)] ?? fills[6] ?? 'gray';
          ctx.fillRect(xa, ya, Math.round(x0 + cellW) - xa, Math.round(yBot) - ya);
        }
      }
      ctx.globalAlpha = 1;
      const cellH = dLat * RAD * v.k;
      if (letters && cellW >= 13 && cellH >= 13) {
        ctx.font = "bold 10px 'B612', sans-serif";
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        for (let i = 0; i < lats.length; i++) {
          for (let j = 0; j < lons.length; j++) {
            const d = grid.dominant[i]?.[j];
            const val = values[i]?.[j];
            if (!d || val === null || val === undefined) continue;
            const x = xOf(lons[j] ?? 0);
            const y = yOf(lats[i] ?? 0);
            if (x < 0 || x > v.w || y < 0 || y > v.h) continue;
            ctx.fillStyle = heatClass(val) <= 3 ? color('--heat-ink-0') : color('--heat-ink-1');
            ctx.fillText(DOMINANT_LETTER[d] ?? '?', x, y);
          }
        }
      }
    }

    // footprints
    if (on('footprints') && p.data.footprints) {
      let feats = footprintFeatures.current.get(p.data.footprints);
      if (!feats) {
        feats = p.data.footprints.map((f) => ringFeature(f.ring));
        footprintFeatures.current.set(p.data.footprints, feats);
      }
      ctx.setLineDash([5, 4]);
      ctx.strokeStyle = color('--sat');
      ctx.lineWidth = 1.2;
      for (const f of feats) {
        ctx.beginPath();
        path(f);
        ctx.stroke();
      }
      ctx.setLineDash([]);
    }

    // lines of position
    if (on('lops') && p.data.lops) {
      let feats = lopFeatures.current.get(p.data.lops);
      if (!feats) {
        feats = p.data.lops.features.filter((f) => f.geometry).map((f) => d3Winding(f));
        lopFeatures.current.set(p.data.lops, feats);
      }
      for (const f of feats) {
        if (f.properties.role !== 'band') continue;
        ctx.beginPath();
        path(f as unknown as Feature<Geometry>);
        ctx.fillStyle = color('--lop-fill');
        ctx.fill();
      }
      for (const f of feats) {
        if (f.properties.role !== 'line') continue;
        ctx.beginPath();
        path(f as unknown as Feature<Geometry>);
        ctx.setLineDash(f.properties.kind === 'fdoa' ? [7, 4] : []);
        ctx.strokeStyle = color('--lop');
        ctx.lineWidth = 1.6;
        ctx.stroke();
      }
      ctx.setLineDash([]);
    }

    // marks first, labels after (so labels can avoid every mark)
    const labeler = new Labeler(ctx);
    const labels: Array<{ text: string; x: number; y: number; r: number; force: boolean; fill: string; font: string }> = [];
    const font = "12px 'B612', sans-serif";

    // fixes and ellipses (mirrors dashed)
    if (on('events') && p.data.solutions) {
      const sols = [...p.data.solutions].sort((a, b) => Number(a.selected) - Number(b.selected));
      for (const s of sols) {
        const ring = ellipseRing(s.lat, s.lon, s.semiMajorM, s.semiMinorM, s.azimuthDeg);
        const f = ringFeature(ring);
        ctx.beginPath();
        path(f);
        if (s.role === 'fix') {
          ctx.fillStyle = color('--fix-fill');
          ctx.fill();
        }
        ctx.setLineDash(s.role === 'mirror' ? [6, 4] : []);
        ctx.strokeStyle = color('--fix');
        ctx.lineWidth = s.selected ? 2.4 : 1.4;
        ctx.stroke();
        ctx.setLineDash([]);
        const pt = proj([s.lon, s.lat]);
        if (!pt) continue;
        const [x, y] = pt;
        const r = s.selected ? 9 : 6;
        ctx.beginPath();
        ctx.moveTo(x - r, y);
        ctx.lineTo(x + r, y);
        ctx.moveTo(x, y - r);
        ctx.lineTo(x, y + r);
        ctx.strokeStyle = color('--fix');
        ctx.lineWidth = 2;
        if (s.role === 'mirror') ctx.setLineDash([3, 2]);
        ctx.stroke();
        ctx.setLineDash([]);
        labeler.mark(x, y, r);
        labels.push({ text: s.label, x, y, r, force: true, fill: color('--fix'), font: s.selected ? "bold 12px 'B612', sans-serif" : font });
      }
    }

    // simulation truth (X marks)
    if (on('truth') && p.data.truth) {
      for (const t of p.data.truth) {
        const pt = proj([t.lon, t.lat]);
        if (!pt) continue;
        const [x, y] = pt;
        const r = t.selected ? 8 : 6;
        ctx.strokeStyle = color('--paper');
        ctx.lineWidth = 4;
        ctx.beginPath();
        ctx.moveTo(x - r, y - r);
        ctx.lineTo(x + r, y + r);
        ctx.moveTo(x + r, y - r);
        ctx.lineTo(x - r, y + r);
        ctx.stroke();
        ctx.strokeStyle = t.enabled === false ? color('--ink-3') : color('--ink');
        ctx.lineWidth = t.selected ? 2.6 : 1.8;
        ctx.stroke();
        if (t.selected) {
          ctx.beginPath();
          ctx.arc(x, y, r + 5, 0, 2 * Math.PI);
          ctx.lineWidth = 1.5;
          ctx.stroke();
        }
        labeler.mark(x, y, r);
        labels.push({ text: t.label, x, y, r, force: true, fill: t.enabled === false ? color('--ink-3') : color('--ink'), font });
      }
    }

    // sites
    if (on('sites') && p.data.sites) {
      for (const s of p.data.sites) {
        const pt = proj([s.lon, s.lat]);
        if (!pt) continue;
        const [x, y] = pt;
        ctx.fillStyle = color('--site');
        ctx.strokeStyle = color('--paper');
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.rect(x - 4, y - 4, 8, 8);
        ctx.fill();
        ctx.stroke();
        labeler.mark(x, y, 5);
        labels.push({ text: s.id, x, y, r: 4, force: false, fill: color('--ink'), font });
      }
    }

    // sub-satellite points
    if (on('satellites') && p.data.subpoints) {
      for (const s of p.data.subpoints) {
        const pt = proj([s.lon_deg, s.lat_deg]);
        if (!pt) continue;
        const [x, y] = pt;
        ctx.strokeStyle = color('--sat');
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.arc(x, y, 6, 0, 2 * Math.PI);
        ctx.stroke();
        ctx.beginPath();
        ctx.moveTo(x - 9, y);
        ctx.lineTo(x + 9, y);
        ctx.moveTo(x, y - 9);
        ctx.lineTo(x, y + 9);
        ctx.stroke();
        labeler.mark(x, y, 9);
        labels.push({ text: s.sat_id, x, y, r: 8, force: false, fill: color('--ink-2'), font });
      }
    }

    // scale bar for the center latitude (Mercator scale is the same in every direction there)
    const mPerPx = (R_EARTH_M * Math.cos(v.lat0 * RAD)) / v.k;
    const barKm = niceKm((mPerPx * 120) / 1000);
    if (barKm > 0) {
      const px = (barKm * 1000) / mPerPx;
      const x0 = 10;
      const y = v.h - 12;
      ctx.strokeStyle = color('--paper');
      ctx.lineWidth = 4;
      ctx.beginPath();
      ctx.moveTo(x0, y - 5);
      ctx.lineTo(x0, y);
      ctx.lineTo(x0 + px, y);
      ctx.lineTo(x0 + px, y - 5);
      ctx.stroke();
      ctx.strokeStyle = color('--ink');
      ctx.lineWidth = 1.5;
      ctx.stroke();
      ctx.font = "12px 'B612', sans-serif";
      ctx.fillStyle = color('--ink');
      ctx.textAlign = 'left';
      ctx.textBaseline = 'alphabetic';
      const latR = Math.round(v.lat0);
      const text = `${barKm >= 1 ? barKm : barKm.toFixed(barKm < 0.1 ? 2 : 1)} km at ${latR === 0 ? 'the equator' : formatLat(latR, 0)}`;
      labeler.markBox({ x0: x0 - 4, y0: y - 12, x1: x0 + px + 10 + ctx.measureText(text).width, y1: y + 6 });
      ctx.lineWidth = 3;
      ctx.strokeStyle = color('--paper');
      ctx.strokeText(text, x0 + px + 6, y + 1);
      ctx.fillText(text, x0 + px + 6, y + 1);
    }

    for (const l of labels) {
      ctx.font = l.font;
      ctx.fillStyle = l.fill;
      labeler.place(l.text, l.x, l.y, l.r, l.force);
    }

    // placement crosshair for keyboard placement
    if (p.placing && document.activeElement === wrapRef.current) {
      ctx.strokeStyle = color('--ink');
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(cx - 14, cy);
      ctx.lineTo(cx + 14, cy);
      ctx.moveTo(cx, cy - 14);
      ctx.lineTo(cx, cy + 14);
      ctx.stroke();
    }

    if (moving) setTimeout(schedule, 160);
  };

  // ---------------------------------------------------------------- size and data changes
  useEffect(() => {
    const wrap = wrapRef.current;
    const canvas = canvasRef.current;
    if (!wrap || !canvas) return;
    const ro = new ResizeObserver(() => {
      const r = wrap.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      canvas.width = Math.max(1, Math.round(r.width * dpr));
      canvas.height = Math.max(1, Math.round(r.height * dpr));
      canvas.style.width = `${r.width}px`;
      canvas.style.height = `${r.height}px`;
      if (!viewRef.current) {
        viewRef.current = { lon0: 150, lat0: 0, k: 200, w: r.width, h: r.height };
        const f = propsRef.current.focus;
        if (f) applyFocus(f);
        else resetView();
      } else {
        viewRef.current.w = r.width;
        viewRef.current.h = r.height;
      }
      schedule();
    });
    ro.observe(wrap);
    const off = onThemeChange(schedule);
    return () => {
      ro.disconnect();
      off();
      cancelAnimationFrame(frame.current);
      frame.current = 0;
    };
  }, []);

  useEffect(schedule, [props.data, props.layers, world, props.placing]);

  function applyFocus(f: MapFocus) {
    fitBounds({ lonMin: f.lonMin, lonMax: f.lonMax, latMin: Math.max(-MAX_LAT, f.latMin), latMax: Math.min(MAX_LAT, f.latMax) }, 0.75);
  }
  useEffect(() => {
    if (props.focus) applyFocus(props.focus);
  }, [props.focus?.key]);

  useEffect(() => {
    if (props.region && viewRef.current && !propsRef.current.focus) resetView();
  }, [props.region?.lon_min, props.region?.lon_max, props.region?.lat_min, props.region?.lat_max]);

  // ---------------------------------------------------------------- interaction
  const drag = useRef<{ x: number; y: number; lon0: number; lat0: number; moved: boolean } | null>(null);

  const toLatLon = (x: number, y: number): [number, number] | null => {
    const v = viewRef.current;
    if (!v) return null;
    const inv = makeProjection(v).invert?.([x, y]);
    if (!inv || !Number.isFinite(inv[0]) || !Number.isFinite(inv[1]) || Math.abs(inv[1]) > MAX_LAT) return null;
    return [inv[1], wrapLon(inv[0])];
  };

  const panBy = (dx: number, dy: number, from?: { lon0: number; lat0: number }) => {
    const v = viewRef.current;
    if (!v) return;
    const lon0 = from?.lon0 ?? v.lon0;
    const lat0 = from?.lat0 ?? v.lat0;
    v.lon0 = wrapLon(lon0 - dx / (v.k * RAD));
    v.lat0 = Math.max(-80, Math.min(80, invMercY(mercY(lat0) + dy / v.k)));
  };

  const zoomAt = (x: number, y: number, factor: number) => {
    const v = viewRef.current;
    if (!v) return;
    const before = toLatLon(x, y);
    v.k = Math.max(80, Math.min(400000, v.k * factor));
    if (before) {
      const after = toLatLon(x, y);
      if (after) {
        v.lon0 = wrapLon(v.lon0 + (before[1] - after[1]));
        v.lat0 = Math.max(-80, Math.min(80, invMercY(mercY(v.lat0) + mercY(before[0]) - mercY(after[0]))));
      }
    }
    interactingUntil.current = performance.now() + 150;
    schedule();
  };

  const pick = (x: number, y: number): { kind: 'solution' | 'truth' | 'site'; id: string } | null => {
    const v = viewRef.current;
    const p = propsRef.current;
    if (!v) return null;
    const proj = makeProjection(v);
    let best: { kind: 'solution' | 'truth' | 'site'; id: string; d: number } | null = null;
    const consider = (kind: 'solution' | 'truth' | 'site', id: string, lat: number, lon: number, bias: number) => {
      const pt = proj([lon, lat]);
      if (!pt) return;
      const d = Math.hypot(pt[0] - x, pt[1] - y) - bias;
      if (d < 12 && (!best || d < best.d)) best = { kind, id, d };
    };
    if (p.layers.truth) for (const t of p.data.truth ?? []) consider('truth', t.id, t.lat, t.lon, 3);
    if (p.layers.events) for (const s of p.data.solutions ?? []) consider('solution', s.eventId, s.lat, s.lon, 2);
    if (p.layers.sites) for (const s of p.data.sites ?? []) consider('site', s.id, s.lat, s.lon, 0);
    return best;
  };

  const readout = (x: number, y: number) => {
    const el = readoutRef.current;
    if (!el) return;
    const ll = toLatLon(x, y);
    if (!ll) {
      el.textContent = '';
      return;
    }
    const [lat, lon] = ll;
    let text = `${formatLat(lat, 3)}  ${formatLon(lon, 3)}  WGS 84`;
    const m = formatMgrs(lat, lon, 3);
    if (m) text += `   MGRS ${m}`;
    const p = propsRef.current;
    if (p.layers.accuracy && p.data.heat) {
      const g = p.data.heat.grid;
      const dLat = (g.lat_deg[1] ?? 0) - (g.lat_deg[0] ?? 0) || 1;
      const dLon = (g.lon_deg[1] ?? 0) - (g.lon_deg[0] ?? 0) || 1;
      const i = Math.round((lat - (g.lat_deg[0] ?? 0)) / dLat);
      let rel = lon - (g.lon_deg[0] ?? 0);
      rel = ((rel % 360) + 360) % 360;
      const j = Math.round(rel / dLon);
      const val = g.semi_major_95_m[i]?.[j];
      const cep = g.cep50_m[i]?.[j];
      const dom = g.dominant[i]?.[j];
      if (i >= 0 && i < g.lat_deg.length && j >= 0 && j < g.lon_deg.length) {
        text += val === null || val === undefined ? '   no estimate possible here' : `   95 % ${km(val)} km, CEP50 ${km(cep)} km, dominant ${DOMINANT_WORD[dom ?? ''] ?? dom ?? '—'}`;
      }
    }
    el.textContent = text;
  };

  const onPointerDown = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const v = viewRef.current;
    if (!v) return;
    (e.target as HTMLCanvasElement).setPointerCapture(e.pointerId);
    drag.current = { x: e.clientX, y: e.clientY, lon0: v.lon0, lat0: v.lat0, moved: false };
  };
  const onPointerMove = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const rect = (e.target as HTMLCanvasElement).getBoundingClientRect();
    readout(e.clientX - rect.left, e.clientY - rect.top);
    const d = drag.current;
    const v = viewRef.current;
    if (!d || !v) return;
    const dx = e.clientX - d.x;
    const dy = e.clientY - d.y;
    if (!d.moved && Math.hypot(dx, dy) < 4) return;
    d.moved = true;
    panBy(dx, dy, d);
    interactingUntil.current = performance.now() + 150;
    schedule();
  };
  const onPointerUp = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const d = drag.current;
    drag.current = null;
    if (!d || d.moved) return;
    const rect = (e.target as HTMLCanvasElement).getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    const p = propsRef.current;
    if (p.placing && p.onMapClick) {
      const ll = toLatLon(x, y);
      if (ll) p.onMapClick(ll[0], ll[1]);
      return;
    }
    const hit = pick(x, y);
    if (hit && p.onPick) p.onPick(hit.kind, hit.id);
    else if (!hit && p.onMapClick) {
      const ll = toLatLon(x, y);
      if (ll) p.onMapClick(ll[0], ll[1]);
    }
  };

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const rect = canvas.getBoundingClientRect();
      zoomAt(e.clientX - rect.left, e.clientY - rect.top, Math.exp(-e.deltaY * 0.0015));
    };
    canvas.addEventListener('wheel', onWheel, { passive: false });
    return () => canvas.removeEventListener('wheel', onWheel);
  }, []);

  const onKeyDown = (e: React.KeyboardEvent) => {
    const v = viewRef.current;
    if (!v || e.target !== wrapRef.current) return;
    const panPx = Math.min(v.w, v.h) * 0.12;
    let handled = true;
    switch (e.key) {
      case 'ArrowLeft':
        panBy(panPx, 0);
        break;
      case 'ArrowRight':
        panBy(-panPx, 0);
        break;
      case 'ArrowUp':
        panBy(0, panPx);
        break;
      case 'ArrowDown':
        panBy(0, -panPx);
        break;
      case '+':
      case '=':
        zoomAt(v.w / 2, v.h / 2, 1.4);
        break;
      case '-':
        zoomAt(v.w / 2, v.h / 2, 1 / 1.4);
        break;
      case '0':
        if (propsRef.current.focus) applyFocus(propsRef.current.focus);
        else resetView();
        break;
      case 'Enter': {
        const p = propsRef.current;
        if (p.placing && p.onMapClick) p.onMapClick(v.lat0, v.lon0);
        else handled = false;
        break;
      }
      default:
        handled = false;
    }
    if (handled) {
      e.preventDefault();
      e.stopPropagation();
      readout(v.w / 2, v.h / 2);
      schedule();
    }
  };

  const toggle = (id: LayerId) => props.onLayers?.({ ...props.layers, [id]: !props.layers[id] });
  const offered = (['events', 'lops', 'truth', 'accuracy', 'sites', 'satellites', 'footprints'] as LayerId[]).filter((id) => props.layers[id] !== undefined);
  const footMin = props.data.footprints?.[0]?.min_elevation_deg;
  const kSigma = props.data.lops?.features[0]?.properties.k_sigma ?? 2;

  return (
    <div className="mapwrap" style={{ flex: 1 }}>
      <div
        className="map"
        ref={wrapRef}
        tabIndex={0}
        role="application"
        aria-label={`${props.label}. Arrow keys pan, plus and minus zoom, 0 resets the view${props.placing ? ', Enter places at the center' : ''}.`}
        data-placing={props.placing ? 'true' : 'false'}
        onKeyDown={onKeyDown}
        onFocus={schedule}
        onBlur={schedule}
      >
        <canvas
          ref={canvasRef}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerLeave={() => readoutRef.current && (readoutRef.current.textContent = '')}
        />
        <div className="mapctl">
          <div className="zoom">
            <button type="button" aria-label="Zoom in" onClick={() => viewRef.current && zoomAt(viewRef.current.w / 2, viewRef.current.h / 2, 1.5)}>
              +
            </button>
            <button type="button" aria-label="Zoom out" onClick={() => viewRef.current && zoomAt(viewRef.current.w / 2, viewRef.current.h / 2, 1 / 1.5)}>
              {'−'}
            </button>
          </div>
          <button type="button" className="btn small" onClick={() => (props.focus ? applyFocus(props.focus) : resetView())}>
            {props.focus ? 'Fit' : 'Region'}
          </button>
        </div>
        {worldError ? <div className="mapnote err-text">Map data did not load: {worldError}</div> : null}
        {props.children}
      </div>
      {offered.length ? (
        <div className="maplegend" role="group" aria-label="Map layers">
          {offered.map((id) => (
            <label key={id}>
              <input type="checkbox" checked={props.layers[id] === true} onChange={() => toggle(id)} />
              <LayerSwatch id={id} />
              <span>
                {layerLabel(id, props)}
                {id === 'footprints' && footMin !== undefined ? ` (${footMin}° elevation)` : ''}
              </span>
              {id === 'lops' && props.layers.lops ? <span className="muted">solid TDOA, dashed FDOA, band ±{kSigma}σ</span> : null}
              {id === 'accuracy' && props.layers.accuracy && props.data.heat ? <HeatLegend field={props.data.heat.field} /> : null}
            </label>
          ))}
        </div>
      ) : null}
      <div className="mapstatus mono" aria-live="off">
        <span ref={readoutRef} />
      </div>
    </div>
  );
}

function LayerSwatch({ id }: { id: LayerId }) {
  const common = { width: 22, height: 14, viewBox: '0 0 22 14', 'aria-hidden': true } as const;
  switch (id) {
    case 'sites':
      return (
        <svg {...common}>
          <rect x={7} y={3} width={8} height={8} style={{ fill: 'var(--site)' }} />
        </svg>
      );
    case 'satellites':
      return (
        <svg {...common}>
          <circle cx={11} cy={7} r={5} style={{ fill: 'none', stroke: 'var(--sat)', strokeWidth: 1.5 }} />
          <path d="M3 7h16M11 0v14" style={{ stroke: 'var(--sat)', strokeWidth: 1.5 }} />
        </svg>
      );
    case 'footprints':
      return (
        <svg {...common}>
          <path d="M1 7h20" style={{ stroke: 'var(--sat)', strokeWidth: 1.2, strokeDasharray: '5 4' }} />
        </svg>
      );
    case 'events':
      return (
        <svg {...common}>
          <ellipse cx={11} cy={7} rx={10} ry={5} style={{ fill: 'var(--fix-fill)', stroke: 'var(--fix)', strokeWidth: 1.6 }} />
          <path d="M6 7h10M11 2v10" style={{ stroke: 'var(--fix)', strokeWidth: 1.6 }} />
        </svg>
      );
    case 'lops':
      return (
        <svg {...common}>
          <rect x={1} y={3} width={20} height={8} style={{ fill: 'var(--lop-fill)' }} />
          <path d="M1 7h20" style={{ stroke: 'var(--lop)', strokeWidth: 1.6 }} />
        </svg>
      );
    case 'accuracy':
      return (
        <svg {...common}>
          {[0, 1, 2, 3, 4].map((i) => (
            <rect key={i} x={1 + i * 4} y={2} width={4} height={10} style={{ fill: `var(--heat-${i})` }} />
          ))}
        </svg>
      );
    case 'truth':
      return (
        <svg {...common}>
          <path d="M6 2l10 10M16 2L6 12" style={{ stroke: 'var(--ink)', strokeWidth: 1.8 }} />
        </svg>
      );
  }
}

function HeatLegend({ field }: { field: MapHeat['field'] }) {
  return (
    <span className="heatlegend" role="img" aria-label={`${field === 'cep50_m' ? 'CEP50' : '95 % semi-major axis'} in km: under 1, 1 to 3, 3 to 10, 10 to 30, 30 to 100, 100 to 300, over 300`}>
      <span className="ink2">{field === 'cep50_m' ? 'CEP50' : '95 %'}</span>
      <span className="heatscale" aria-hidden="true">
        <span className="heatbar">
          {[0, 1, 2, 3, 4, 5, 6].map((i) => (
            <span key={i} style={{ background: `var(--heat-${i})` }} />
          ))}
        </span>
        <span className="heatticks num">
          {HEAT_EDGES_KM.map((e, i) => (
            <span key={e} style={{ left: `${((i + 1) * 100) / 7}%` }}>
              {e}
            </span>
          ))}
        </span>
      </span>
      <span className="ink2">km</span>
    </span>
  );
}
