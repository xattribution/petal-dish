// Spectrum drawing outside React's render cycle. Frames go from the socket straight into
// per-window state (latest trace, baseline, detections, a waterfall image that scrolls by
// one row per frame); one requestAnimationFrame loop repaints only the panels that
// changed. About 20 windows at 2 Hz is a few hundred microseconds of work per frame.
import type { PsdDetection, PsdFrame } from '../api/plb1';
import { mhz } from '../format/units';
import { color } from '../styles/theme';
import { formatAge } from '../time/dtg';

export interface Band {
  lo: number;
  hi: number;
  label: string;
}

export interface WindowMeta {
  key: string;
  channelId: string;
  monitorId: string;
  siteId: string;
  centerHz: number;
  spanHz: number;
  nfft: number;
  frameRateHz: number;
  transponders: Band[];
  carriers: Band[];
}

interface Panel {
  trace: HTMLCanvasElement;
  wf: HTMLCanvasElement;
  readout: HTMLElement;
  age: HTMLElement;
  root: HTMLElement;
}

const WF_ROWS = 120;
const WF_MAX_W = 1024;

class WindowState {
  psd: Float32Array | null = null;
  baseline: Float32Array | null = null;
  detections: PsdDetection[] = [];
  center: number;
  span: number;
  lastAt = 0;
  frames = 0;
  wf: HTMLCanvasElement;
  wfCtx: CanvasRenderingContext2D | null;
  row: ImageData | null = null;
  panel: Panel | null = null;
  dirty = true;
  hoverX: number | null = null;
  floor = 0;

  constructor(public meta: WindowMeta) {
    this.center = meta.centerHz;
    this.span = meta.spanHz;
    this.wf = document.createElement('canvas');
    this.wf.width = Math.min(meta.nfft, WF_MAX_W);
    this.wf.height = WF_ROWS;
    this.wfCtx = this.wf.getContext('2d');
    if (this.wfCtx) {
      this.wfCtx.fillStyle = color('--wf-lo');
      this.wfCtx.fillRect(0, 0, this.wf.width, WF_ROWS);
    }
  }
}

function parseHex(c: string): [number, number, number] {
  const m = c.trim().match(/^#?([0-9a-f]{6})/i);
  if (!m || !m[1]) return [128, 128, 128];
  const n = parseInt(m[1], 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function median(a: Float32Array): number {
  const s = Float32Array.from(a).sort();
  return s[Math.floor(s.length / 2)] ?? 0;
}

export class SpectrumEngine {
  private states = new Map<string, WindowState>();
  private raf = 0;
  private ageTimer: ReturnType<typeof setInterval> | null = null;
  private lut: Uint8ClampedArray = new Uint8ClampedArray(256 * 3);
  /** dB span of the trace and waterfall above the floor */
  rangeDb = 36;
  private offlineSites = new Set<string>();

  /** Sites whose node is offline: their windows say so instead of waiting for frames. */
  setOfflineSites(sites: Set<string>): void {
    this.offlineSites = sites;
  }

  constructor() {
    this.buildLut();
  }

  buildLut(): void {
    const lo = parseHex(color('--wf-lo'));
    const hi = parseHex(color('--wf-hi'));
    for (let i = 0; i < 256; i++) {
      const t = Math.pow(i / 255, 0.85);
      for (let c = 0; c < 3; c++) this.lut[i * 3 + c] = Math.round((lo[c] ?? 0) + ((hi[c] ?? 0) - (lo[c] ?? 0)) * t);
    }
    for (const s of this.states.values()) s.dirty = true;
  }

  setWindows(metas: WindowMeta[]): void {
    const keep = new Set(metas.map((m) => m.key));
    for (const k of [...this.states.keys()]) if (!keep.has(k)) this.states.delete(k);
    for (const m of metas) {
      const s = this.states.get(m.key);
      if (!s) this.states.set(m.key, new WindowState(m));
      else s.meta = m;
    }
  }

  attach(key: string, panel: Panel): void {
    const s = this.states.get(key);
    if (!s) return;
    s.panel = panel;
    s.dirty = true;
    const move = (e: PointerEvent) => {
      const r = panel.trace.getBoundingClientRect();
      s.hoverX = e.clientX - r.left;
      s.dirty = true;
    };
    const leave = () => {
      s.hoverX = null;
      s.dirty = true;
    };
    panel.trace.onpointermove = move;
    panel.trace.onpointerleave = leave;
    panel.wf.onpointermove = move;
    panel.wf.onpointerleave = leave;
  }

  detach(key: string): void {
    const s = this.states.get(key);
    if (s?.panel) {
      s.panel.trace.onpointermove = null;
      s.panel.wf.onpointermove = null;
    }
    if (s) s.panel = null;
  }

  frame(f: PsdFrame): void {
    const key = `${f.header.channel_id}\u0000${f.header.monitor_id}`;
    const s = this.states.get(key);
    if (!s) return;
    // keep copies: the frame buffer is reused by nobody, but the views alias it
    s.psd = Float32Array.from(f.psd);
    s.baseline = f.baseline ? Float32Array.from(f.baseline) : null;
    s.detections = f.header.detections;
    s.center = f.header.center_hz;
    s.span = f.header.span_hz;
    s.lastAt = performance.now();
    s.frames += 1;
    s.floor = median(s.baseline ?? s.psd) - 4;
    this.pushRow(s);
    s.dirty = true;
  }

  private pushRow(s: WindowState): void {
    const ctx = s.wfCtx;
    const psd = s.psd;
    if (!ctx || !psd) return;
    const w = s.wf.width;
    if (!s.row || s.row.width !== w) s.row = ctx.createImageData(w, 1);
    const data = s.row.data;
    const per = psd.length / w;
    for (let x = 0; x < w; x++) {
      let v = -Infinity;
      const k0 = Math.floor(x * per);
      const k1 = Math.max(k0 + 1, Math.floor((x + 1) * per));
      for (let k = k0; k < k1; k++) v = Math.max(v, psd[k] ?? -Infinity);
      const t = Math.max(0, Math.min(255, Math.round(((v - s.floor) / this.rangeDb) * 255)));
      data[x * 4] = this.lut[t * 3] ?? 0;
      data[x * 4 + 1] = this.lut[t * 3 + 1] ?? 0;
      data[x * 4 + 2] = this.lut[t * 3 + 2] ?? 0;
      data[x * 4 + 3] = 255;
    }
    ctx.drawImage(s.wf, 0, 0, w, WF_ROWS - 1, 0, 1, w, WF_ROWS - 1);
    ctx.putImageData(s.row, 0, 0);
  }

  start(): void {
    const loop = () => {
      this.raf = requestAnimationFrame(loop);
      for (const s of this.states.values()) {
        if (s.dirty && s.panel) {
          s.dirty = false;
          this.draw(s);
        }
      }
    };
    this.raf = requestAnimationFrame(loop);
    this.ageTimer = setInterval(() => this.ages(), 500);
  }

  stop(): void {
    cancelAnimationFrame(this.raf);
    if (this.ageTimer) clearInterval(this.ageTimer);
  }

  private ages(): void {
    const now = performance.now();
    for (const s of this.states.values()) {
      if (!s.panel) continue;
      let text: string;
      let st: string;
      const offline = this.offlineSites.has(s.meta.siteId);
      if (!s.frames) {
        text = offline ? 'node offline, no frames' : 'waiting for the first frame';
        st = 'nodata';
      } else {
        const age = (now - s.lastAt) / 1000;
        const stale = age > 3 / Math.max(0.1, s.meta.frameRateHz);
        text = stale ? `${offline ? 'node offline, ' : 'stale, '}last frame ${formatAge(age)} ago` : `frame ${formatAge(age)} ago`;
        st = stale ? 'stale' : 'live';
      }
      if (s.panel.age.textContent !== text) s.panel.age.textContent = text;
      if (s.panel.root.dataset.state !== st) s.panel.root.dataset.state = st;
    }
  }

  private sizeCanvas(c: HTMLCanvasElement): [CanvasRenderingContext2D | null, number, number] {
    const dpr = window.devicePixelRatio || 1;
    const w = c.clientWidth;
    const h = c.clientHeight;
    if (c.width !== Math.round(w * dpr) || c.height !== Math.round(h * dpr)) {
      c.width = Math.round(w * dpr);
      c.height = Math.round(h * dpr);
    }
    const ctx = c.getContext('2d');
    ctx?.setTransform(dpr, 0, 0, dpr, 0, 0);
    return [ctx, w, h];
  }

  private draw(s: WindowState): void {
    const p = s.panel;
    if (!p) return;
    const [ctx, w, h] = this.sizeCanvas(p.trace);
    if (!ctx || w === 0) return;
    const left = 34;
    const right = 6;
    const top = 24; // frequency-plan strip above the plot: carrier brackets and ids
    const bottom = 16;
    const pw = w - left - right;
    const ph = h - top - bottom;
    const lo = s.center - s.span / 2;
    const xOf = (f: number) => left + ((f - lo) / s.span) * pw;
    const yOf = (db: number) => top + ph - ((db - s.floor) / this.rangeDb) * ph;

    ctx.clearRect(0, 0, w, h);
    ctx.font = "11px 'B612 Mono', monospace";
    ctx.textBaseline = 'middle';
    // level grid every 10 dB
    ctx.strokeStyle = color('--rule');
    ctx.fillStyle = color('--ink-3');
    ctx.lineWidth = 1;
    ctx.textAlign = 'right';
    const g0 = Math.ceil(s.floor / 10) * 10;
    for (let d = g0; d <= s.floor + this.rangeDb; d += 10) {
      const y = Math.round(yOf(d)) + 0.5;
      ctx.beginPath();
      ctx.moveTo(left, y);
      ctx.lineTo(w - right, y);
      ctx.stroke();
      ctx.fillText(d < 0 ? `−${-d}` : `${d}`, left - 4, y);
    }
    // transponder edges (ids in the strip only when the window holds more than one)
    ctx.setLineDash([3, 3]);
    ctx.strokeStyle = color('--ink-3');
    ctx.fillStyle = color('--ink-3');
    ctx.textAlign = 'center';
    const tps = s.meta.transponders.filter((b) => b.hi >= lo && b.lo <= lo + s.span);
    for (const b of tps) {
      for (const f of [b.lo, b.hi]) {
        if (f < lo || f > lo + s.span) continue;
        const x = Math.round(xOf(f)) + 0.5;
        ctx.beginPath();
        ctx.moveTo(x, top);
        ctx.lineTo(x, top + ph);
        ctx.stroke();
      }
      if (tps.length > 1) ctx.fillText(b.label, xOf((Math.max(b.lo, lo) + Math.min(b.hi, lo + s.span)) / 2), top + 8);
    }
    ctx.setLineDash([]);
    // detections: shaded band, bar on top
    for (const d of s.detections) {
      const x0 = xOf(d.f_lo_hz);
      const x1 = xOf(d.f_hi_hz);
      ctx.fillStyle = color('--det-fill');
      ctx.fillRect(x0, top, Math.max(2, x1 - x0), ph);
      ctx.fillStyle = color('--fix');
      ctx.fillRect(x0, top, Math.max(2, x1 - x0), 2);
    }
    // authorized carriers: a bracket over the allocation in the plan strip, id above it
    ctx.strokeStyle = color('--ink-2');
    ctx.fillStyle = color('--ink-2');
    ctx.lineWidth = 1;
    for (const c of s.meta.carriers) {
      if (c.hi < lo || c.lo > lo + s.span) continue;
      const x0 = Math.round(xOf(Math.max(c.lo, lo))) + 0.5;
      const x1 = Math.round(xOf(Math.min(c.hi, lo + s.span))) + 0.5;
      ctx.beginPath();
      ctx.moveTo(x0, top - 2);
      ctx.lineTo(x0, top - 6);
      ctx.lineTo(x1, top - 6);
      ctx.lineTo(x1, top - 2);
      ctx.stroke();
      ctx.textAlign = 'center';
      ctx.fillText(c.label, (x0 + x1) / 2, top - 15);
    }
    // baseline
    const psd = s.psd;
    const n = psd?.length ?? s.meta.nfft;
    const per = n / pw;
    if (s.baseline) {
      ctx.beginPath();
      ctx.setLineDash([4, 3]);
      ctx.strokeStyle = color('--ink-2');
      for (let x = 0; x < pw; x++) {
        const k = Math.min(n - 1, Math.floor((x + 0.5) * per));
        const y = yOf(s.baseline[k] ?? 0);
        if (x === 0) ctx.moveTo(left + x, y);
        else ctx.lineTo(left + x, y);
      }
      ctx.stroke();
      ctx.setLineDash([]);
    }
    // trace (peak per pixel column)
    if (psd) {
      ctx.beginPath();
      ctx.strokeStyle = color('--ink');
      ctx.lineWidth = 1.2;
      for (let x = 0; x < pw; x++) {
        const k0 = Math.floor(x * per);
        const k1 = Math.max(k0 + 1, Math.floor((x + 1) * per));
        let v = -Infinity;
        for (let k = k0; k < k1 && k < n; k++) v = Math.max(v, psd[k] ?? -Infinity);
        const y = Math.max(top, Math.min(top + ph, yOf(v)));
        if (x === 0) ctx.moveTo(left + x, y);
        else ctx.lineTo(left + x, y);
      }
      ctx.stroke();
    }
    // frequency axis
    ctx.fillStyle = color('--ink-3');
    ctx.textBaseline = 'alphabetic';
    ctx.textAlign = 'left';
    ctx.fillText(`−${(s.span / 2000).toFixed(1)} kHz`, left, h - 3);
    ctx.textAlign = 'center';
    ctx.fillText(`${mhz(s.center)} MHz`, left + pw / 2, h - 3);
    ctx.textAlign = 'right';
    ctx.fillText(`+${(s.span / 2000).toFixed(1)} kHz`, w - right, h - 3);

    // hover marker and readout
    let readout = '';
    if (s.hoverX !== null && s.hoverX >= left && s.hoverX <= left + pw) {
      const x = s.hoverX;
      ctx.strokeStyle = color('--ink');
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(Math.round(x) + 0.5, top);
      ctx.lineTo(Math.round(x) + 0.5, top + ph);
      ctx.stroke();
      const f = lo + ((x - left) / pw) * s.span;
      const k = Math.max(0, Math.min(n - 1, Math.floor(((x - left) / pw) * n)));
      if (psd) {
        const v = psd[k] ?? 0;
        const b = s.baseline?.[k];
        readout = `${mhz(f)} MHz   ${v.toFixed(1)} dB`;
        if (b !== undefined) readout += `   baseline ${b.toFixed(1)} dB   ${(v - b >= 0 ? '+' : '−') + Math.abs(v - b).toFixed(1)} dB`;
        const det = s.detections.find((d) => f >= d.f_lo_hz && f <= d.f_hi_hz);
        if (det) readout += `   detection ${det.track_id}${det.authorized_carrier_id ? ` (${det.authorized_carrier_id}${det.anomaly ? ', anomaly' : ''})` : ''}`;
      } else readout = `${mhz(f)} MHz`;
    }
    if (p.readout.textContent !== readout) p.readout.textContent = readout || ' ';

    // waterfall
    const [wctx, ww, wh] = this.sizeCanvas(p.wf);
    if (wctx && ww > 0) {
      wctx.imageSmoothingEnabled = false;
      wctx.clearRect(0, 0, ww, wh);
      wctx.drawImage(s.wf, 0, 0, s.wf.width, Math.min(WF_ROWS, Math.ceil(wh)), left, 0, pw, wh);
      if (s.hoverX !== null && s.hoverX >= left && s.hoverX <= left + pw) {
        wctx.strokeStyle = color('--fix');
        wctx.beginPath();
        wctx.moveTo(Math.round(s.hoverX) + 0.5, 0);
        wctx.lineTo(Math.round(s.hoverX) + 0.5, wh);
        wctx.stroke();
      }
      wctx.fillStyle = color('--ink-3');
      wctx.font = "11px 'B612 Mono', monospace";
      wctx.textAlign = 'right';
      wctx.textBaseline = 'top';
      wctx.fillText('now', left - 4, 2);
      wctx.textBaseline = 'bottom';
      wctx.fillText(`−${Math.round(wh / Math.max(0.5, s.meta.frameRateHz))} s`, left - 4, wh - 1);
    }
  }
}
