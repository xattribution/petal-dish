// Small canvas plots for the event dossier: line plots (correlation cuts, ellipse history)
// and a heat map (correlation surface). Redrawn on data or theme change; hover readouts are
// written straight into a text element.
import { useEffect, useRef } from 'react';
import { color, onThemeChange } from '../styles/theme';

export interface Series {
  x: number[];
  y: number[];
  token: string;
  dash?: number[];
  label: string;
  points?: boolean;
}

export interface LinePlotProps {
  series: Series[];
  xLabel: string;
  yLabel: string;
  logY?: boolean;
  xFormat?: (v: number) => string;
  yFormat?: (v: number) => string;
  marker?: number | null;
  readout?: (x: number) => string;
  ariaLabel: string;
  xTicks?: number[];
}

function niceTicks(lo: number, hi: number, n = 5): number[] {
  if (!(hi > lo)) return [lo];
  const span = hi - lo;
  const step0 = Math.pow(10, Math.floor(Math.log10(span / n)));
  const err = span / n / step0;
  const step = step0 * (err >= 7.5 ? 10 : err >= 3.5 ? 5 : err >= 1.5 ? 2 : 1);
  const out: number[] = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) out.push(Math.abs(v) < step * 1e-9 ? 0 : v);
  return out;
}

function prep(c: HTMLCanvasElement): [CanvasRenderingContext2D | null, number, number] {
  const dpr = window.devicePixelRatio || 1;
  const w = c.clientWidth;
  const h = c.clientHeight;
  c.width = Math.round(w * dpr);
  c.height = Math.round(h * dpr);
  const ctx = c.getContext('2d');
  ctx?.setTransform(dpr, 0, 0, dpr, 0, 0);
  return [ctx, w, h];
}

export function LinePlot(p: LinePlotProps) {
  const ref = useRef<HTMLCanvasElement>(null);
  const out = useRef<HTMLDivElement>(null);
  const hover = useRef<number | null>(null);
  const layout = useRef<{ x0: number; x1: number; px0: number; pw: number } | null>(null);

  const draw = () => {
    const c = ref.current;
    if (!c) return;
    const [ctx, w, h] = prep(c);
    if (!ctx || w === 0) return;
    const left = 50;
    const right = 10;
    const top = 8;
    const bottom = 30;
    const pw = w - left - right;
    const ph = h - top - bottom;
    const xs = p.series.flatMap((s) => s.x);
    const ys = p.series.flatMap((s) => s.y).filter((v) => Number.isFinite(v) && (!p.logY || v > 0));
    if (!xs.length || !ys.length) return;
    let x0 = Math.min(...xs);
    let x1 = Math.max(...xs);
    if (x1 === x0) {
      x0 -= 1;
      x1 += 1;
    }
    const tf = (v: number) => (p.logY ? Math.log10(v) : v);
    let y0 = tf(Math.min(...ys));
    let y1 = tf(Math.max(...ys));
    const pad = (y1 - y0) * 0.08 || 1;
    y0 -= pad;
    y1 += pad;
    layout.current = { x0, x1, px0: left, pw };
    const X = (v: number) => left + ((v - x0) / (x1 - x0)) * pw;
    const Y = (v: number) => top + ph - ((tf(v) - y0) / (y1 - y0)) * ph;
    ctx.clearRect(0, 0, w, h);
    ctx.font = "11px 'B612 Mono', monospace";
    ctx.fillStyle = color('--ink-3');
    ctx.strokeStyle = color('--rule');
    ctx.lineWidth = 1;
    const yTicks = p.logY
      ? niceTicks(Math.floor(y0), Math.ceil(y1), 4).filter((v) => Number.isInteger(v)).map((v) => 10 ** v)
      : niceTicks(y0, y1, 4);
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    for (const t of yTicks) {
      const y = Math.round(Y(t)) + 0.5;
      if (y < top - 1 || y > top + ph + 1) continue;
      ctx.beginPath();
      ctx.moveTo(left, y);
      ctx.lineTo(left + pw, y);
      ctx.stroke();
      ctx.fillText(p.yFormat ? p.yFormat(t) : String(t), left - 4, y);
    }
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    for (const t of p.xTicks ?? niceTicks(x0, x1, 4)) {
      const x = Math.round(X(t)) + 0.5;
      if (x < left - 1 || x > left + pw + 1) continue;
      ctx.beginPath();
      ctx.moveTo(x, top);
      ctx.lineTo(x, top + ph);
      ctx.stroke();
      ctx.fillText(p.xFormat ? p.xFormat(t) : String(t), x, top + ph + 3);
    }
    ctx.fillStyle = color('--ink-2');
    ctx.fillText(p.xLabel, left + pw / 2, h - 12);
    ctx.save();
    ctx.translate(10, top + ph / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.textBaseline = 'middle';
    ctx.fillText(p.yLabel, 0, 0);
    ctx.restore();
    for (const s of p.series) {
      ctx.strokeStyle = color(s.token);
      ctx.fillStyle = color(s.token);
      ctx.lineWidth = 1.5;
      ctx.setLineDash(s.dash ?? []);
      ctx.beginPath();
      s.x.forEach((xv, i) => {
        const yv = s.y[i] ?? NaN;
        if (!Number.isFinite(yv) || (p.logY && yv <= 0)) return;
        const px = X(xv);
        const py = Math.max(top, Math.min(top + ph, Y(yv)));
        if (i === 0) ctx.moveTo(px, py);
        else ctx.lineTo(px, py);
      });
      ctx.stroke();
      ctx.setLineDash([]);
      if (s.points) {
        s.x.forEach((xv, i) => {
          const yv = s.y[i] ?? NaN;
          if (!Number.isFinite(yv)) return;
          ctx.beginPath();
          ctx.arc(X(xv), Y(yv), 3, 0, 2 * Math.PI);
          ctx.fill();
        });
      }
    }
    if (p.marker !== null && p.marker !== undefined && p.marker >= x0 && p.marker <= x1) {
      ctx.strokeStyle = color('--fix');
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(Math.round(X(p.marker)) + 0.5, top);
      ctx.lineTo(Math.round(X(p.marker)) + 0.5, top + ph);
      ctx.stroke();
      ctx.setLineDash([]);
    }
    if (hover.current !== null) {
      ctx.strokeStyle = color('--ink');
      ctx.beginPath();
      ctx.moveTo(Math.round(hover.current) + 0.5, top);
      ctx.lineTo(Math.round(hover.current) + 0.5, top + ph);
      ctx.stroke();
    }
  };

  useEffect(() => {
    draw();
    const off = onThemeChange(draw);
    const ro = new ResizeObserver(draw);
    if (ref.current) ro.observe(ref.current);
    return () => {
      off();
      ro.disconnect();
    };
  });

  return (
    <>
      <canvas
        ref={ref}
        role="img"
        aria-label={p.ariaLabel}
        onPointerMove={(e) => {
          const l = layout.current;
          if (!l) return;
          const x = e.clientX - (e.target as HTMLCanvasElement).getBoundingClientRect().left;
          if (x < l.px0 || x > l.px0 + l.pw) return;
          hover.current = x;
          const xv = l.x0 + ((x - l.px0) / l.pw) * (l.x1 - l.x0);
          if (out.current && p.readout) out.current.textContent = p.readout(xv);
          draw();
        }}
        onPointerLeave={() => {
          hover.current = null;
          if (out.current) out.current.textContent = ' ';
          draw();
        }}
      />
      <div className="readout mono" ref={out}>
        {' '}
      </div>
    </>
  );
}

export interface HeatmapProps {
  /** z[i][j] at y[i], x[j] */
  z: number[][];
  x: number[];
  y: number[];
  xLabel: string;
  yLabel: string;
  xFormat: (v: number) => string;
  yFormat: (v: number) => string;
  zFormat: (v: number) => string;
  peak?: { x: number; y: number } | null;
  ariaLabel: string;
}

/** Correlation surface: grayscale ramp (saturated hues are reserved for status), peak marked in --fix. */
export function Heatmap(p: HeatmapProps) {
  const ref = useRef<HTMLCanvasElement>(null);
  const out = useRef<HTMLDivElement>(null);
  const geom = useRef<{ left: number; top: number; pw: number; ph: number } | null>(null);

  const draw = () => {
    const c = ref.current;
    if (!c) return;
    const [ctx, w, h] = prep(c);
    if (!ctx || w === 0 || !p.z.length) return;
    const left = 56;
    const right = 8;
    const top = 8;
    const bottom = 30;
    const pw = w - left - right;
    const ph = h - top - bottom;
    geom.current = { left, top, pw, ph };
    const flat = p.z.flat().filter(Number.isFinite);
    const zmax = Math.max(...flat);
    const zmin = Math.max(Math.min(...flat), zmax - 30);
    const lo = color('--wf-lo');
    const hi = color('--wf-hi');
    const parse = (cc: string) => {
      const n = parseInt(cc.replace('#', '').slice(0, 6), 16);
      return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
    };
    const [r0, g0, b0] = parse(lo);
    const [r1, g1, b1] = parse(hi);
    const ny = p.z.length;
    const nx = p.z[0]?.length ?? 0;
    const img = ctx.createImageData(nx, ny);
    for (let i = 0; i < ny; i++) {
      for (let j = 0; j < nx; j++) {
        const v = p.z[i]?.[j] ?? zmin;
        const t = Math.max(0, Math.min(1, (v - zmin) / (zmax - zmin || 1)));
        const o = ((ny - 1 - i) * nx + j) * 4;
        img.data[o] = (r0 ?? 0) + ((r1 ?? 0) - (r0 ?? 0)) * t;
        img.data[o + 1] = (g0 ?? 0) + ((g1 ?? 0) - (g0 ?? 0)) * t;
        img.data[o + 2] = (b0 ?? 0) + ((b1 ?? 0) - (b0 ?? 0)) * t;
        img.data[o + 3] = 255;
      }
    }
    const off = document.createElement('canvas');
    off.width = nx;
    off.height = ny;
    off.getContext('2d')?.putImageData(img, 0, 0);
    ctx.clearRect(0, 0, w, h);
    ctx.imageSmoothingEnabled = false;
    // rows are delay (y), columns Doppler (x): draw with delay on the vertical axis
    ctx.drawImage(off, left, top, pw, ph);
    ctx.font = "11px 'B612 Mono', monospace";
    ctx.fillStyle = color('--ink-3');
    const xa = p.x;
    const ya = p.y;
    const X = (v: number) => left + ((v - (xa[0] ?? 0)) / ((xa[xa.length - 1] ?? 1) - (xa[0] ?? 0) || 1)) * pw;
    const Y = (v: number) => top + ph - ((v - (ya[0] ?? 0)) / ((ya[ya.length - 1] ?? 1) - (ya[0] ?? 0) || 1)) * ph;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    for (const t of niceTicks(xa[0] ?? 0, xa[xa.length - 1] ?? 1, 3)) ctx.fillText(p.xFormat(t), X(t), top + ph + 3);
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    for (const t of niceTicks(ya[0] ?? 0, ya[ya.length - 1] ?? 1, 3)) ctx.fillText(p.yFormat(t), left - 4, Y(t));
    ctx.fillStyle = color('--ink-2');
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    ctx.fillText(p.xLabel, left + pw / 2, h - 12);
    ctx.save();
    ctx.translate(10, top + ph / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.textBaseline = 'middle';
    ctx.fillText(p.yLabel, 0, 0);
    ctx.restore();
    if (p.peak) {
      const px = X(p.peak.x);
      const py = Y(p.peak.y);
      ctx.strokeStyle = color('--fix');
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.arc(px, py, 7, 0, 2 * Math.PI);
      ctx.stroke();
    }
  };

  useEffect(() => {
    draw();
    const off = onThemeChange(draw);
    const ro = new ResizeObserver(draw);
    if (ref.current) ro.observe(ref.current);
    return () => {
      off();
      ro.disconnect();
    };
  });

  return (
    <>
      <canvas
        ref={ref}
        role="img"
        aria-label={p.ariaLabel}
        onPointerMove={(e) => {
          const g = geom.current;
          if (!g || !out.current) return;
          const r = (e.target as HTMLCanvasElement).getBoundingClientRect();
          const fx = (e.clientX - r.left - g.left) / g.pw;
          const fy = 1 - (e.clientY - r.top - g.top) / g.ph;
          if (fx < 0 || fx > 1 || fy < 0 || fy > 1) return;
          const j = Math.min(p.x.length - 1, Math.floor(fx * p.x.length));
          const i = Math.min(p.y.length - 1, Math.floor(fy * p.y.length));
          const v = p.z[i]?.[j];
          out.current.textContent = `${p.yLabel} ${p.yFormat(p.y[i] ?? 0)}   ${p.xLabel} ${p.xFormat(p.x[j] ?? 0)}   ${v === undefined ? '' : p.zFormat(v)}`;
        }}
        onPointerLeave={() => out.current && (out.current.textContent = ' ')}
      />
      <div className="readout mono" ref={out}>
        {' '}
      </div>
    </>
  );
}
