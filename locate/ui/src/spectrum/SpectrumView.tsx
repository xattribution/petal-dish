// Live spectrum: one panel per monitor window (trace over waterfall), filtered by site,
// satellite and transponder. Only windows on screen are subscribed (PROTOCOL.md section B
// subscribe); drawing happens in SpectrumEngine, not in React.
import { memo, useEffect, useMemo, useRef } from 'react';
import type { SystemConfig } from '../api/types.gen';
import { Empty, ErrorNote, Loading } from '../components/common';
import { allWindows, downlinkCenter, type WindowInfo } from '../domain/config';
import { setParams, useRoute } from '../router';
import { configResource } from '../state/resources';
import { onFrame, socket, useLive } from '../state/store';
import { onThemeChange } from '../styles/theme';
import { SpectrumEngine, type Band, type WindowMeta } from './engine';

function metaFor(cfg: SystemConfig, w: WindowInfo): WindowMeta {
  const lo = w.monitor.center_hz - w.monitor.span_hz / 2;
  const hi = w.monitor.center_hz + w.monitor.span_hz / 2;
  const transponders: Band[] = [];
  const carriers: Band[] = [];
  const watched = 'satellite_id' in w.channel.pointing ? [w.channel.pointing.satellite_id] : w.channel.pointing.satellite_ids;
  for (const sat of cfg.satellites) {
    if (!watched.includes(sat.id)) continue;
    for (const tp of sat.transponders) {
      const c = downlinkCenter(tp);
      const tlo = c - tp.bandwidth_hz / 2;
      const thi = c + tp.bandwidth_hz / 2;
      if (thi < lo || tlo > hi) continue;
      transponders.push({ lo: tlo, hi: thi, label: tp.id });
      for (const car of cfg.carriers ?? []) {
        if (Math.abs(car.uplink_center_hz - tp.uplink_center_hz) > tp.bandwidth_hz / 2) continue;
        const d = car.uplink_center_hz + tp.translation_hz;
        carriers.push({ lo: d - car.bandwidth_hz / 2, hi: d + car.bandwidth_hz / 2, label: car.id });
      }
    }
  }
  return {
    key: w.key,
    channelId: w.channel.id,
    monitorId: w.monitor.id,
    siteId: w.site.id,
    centerHz: w.monitor.center_hz,
    spanHz: w.monitor.span_hz,
    nfft: w.monitor.nfft ?? 1024,
    frameRateHz: w.monitor.frame_rate_hz ?? 2,
    transponders,
    carriers,
  };
}

const Panel = memo(function Panel(props: { w: WindowInfo; engine: SpectrumEngine; offline: boolean; observe: (el: Element | null, key: string) => void }) {
  const { w, engine } = props;
  const root = useRef<HTMLElement>(null);
  const trace = useRef<HTMLCanvasElement>(null);
  const wf = useRef<HTMLCanvasElement>(null);
  const readout = useRef<HTMLDivElement>(null);
  const age = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (root.current && trace.current && wf.current && readout.current && age.current) {
      engine.attach(w.key, { root: root.current, trace: trace.current, wf: wf.current, readout: readout.current, age: age.current });
    }
    props.observe(root.current, w.key);
    return () => {
      engine.detach(w.key);
      props.observe(null, w.key);
    };
  }, [engine, w.key]);
  return (
    <section className="specpanel" ref={root} data-key={w.key} aria-label={`Spectrum ${w.channel.id} ${w.monitor.id}`}>
      <header>
        <span className="ch mono">{w.channel.id}</span>
        <span className="meta mono">{w.monitor.id}</span>
        <span className="age" ref={age}>
          {props.offline ? 'node offline' : 'no frames yet'}
        </span>
      </header>
      <canvas className="trace" ref={trace} aria-label={`Trace of ${w.channel.id} ${w.monitor.id}: level in dB over frequency, thermal noise = 0 dB. Left and right arrows move the marker; the readout below follows it.`} />
      <canvas className="wf" ref={wf} aria-label="Waterfall: newest row at the top" />
      <div className="readout mono" ref={readout} aria-live="off">
        {' '}
      </div>
    </section>
  );
});

export function SpectrumView() {
  const { data, error, reload } = configResource.use();
  const route = useRoute();
  const nodes = useLive((s) => s.status?.nodes ?? null);
  const site = route.params.get('site') ?? '';
  const sat = route.params.get('sat') ?? '';
  const tp = route.params.get('tp') ?? '';
  const cols = Number(route.params.get('cols') ?? 3);
  const dense = route.params.get('dense') === '1';
  const cfg = data?.envelope.config ?? null;

  const windows = useMemo(() => (cfg ? allWindows(cfg) : []), [cfg]);
  const shown = useMemo(
    () =>
      windows.filter(
        (w) => (!site || w.site.id === site) && (!sat || w.monitor.id.startsWith(`${sat}:`) || ('satellite_id' in w.channel.pointing && w.channel.pointing.satellite_id === sat)) && (!tp || w.monitor.id.endsWith(`:${tp}`)),
      ),
    [windows, site, sat, tp],
  );
  const offline = useMemo(() => new Set((nodes ?? []).filter((n) => !n.online).map((n) => n.site_id)), [nodes]);

  const engineRef = useRef<SpectrumEngine | null>(null);
  if (!engineRef.current) engineRef.current = new SpectrumEngine();
  const engine = engineRef.current;

  // registered during render: the panels' effects (which attach canvases) run before ours
  useMemo(() => {
    if (cfg) engine.setWindows(windows.map((w) => metaFor(cfg, w)));
  }, [cfg, windows, engine]);

  useEffect(() => {
    engine.start();
    const off = onFrame((f) => engine.frame(f));
    const offTheme = onThemeChange(() => engine.buildLut());
    return () => {
      off();
      offTheme();
      engine.stop();
      socket.subscribe([]);
    };
  }, [engine]);

  useEffect(() => engine.setOfflineSites(offline), [engine, offline]);

  // subscribe to the windows on screen
  const visible = useRef(new Set<string>());
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const io = useRef<IntersectionObserver | null>(null);
  const els = useRef(new Map<string, Element>());
  const flush = () => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => {
      socket.subscribe([...visible.current].map((k) => k.split('\u0000') as [string, string]));
    }, 120);
  };
  if (!io.current && typeof IntersectionObserver !== 'undefined') {
    io.current = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          const key = (e.target as HTMLElement).dataset.key;
          if (!key) continue;
          if (e.isIntersecting) visible.current.add(key);
          else visible.current.delete(key);
        }
        flush();
      },
      { rootMargin: '100px 0px' },
    );
  }
  const observe = useMemo(
    () => (el: Element | null, key: string) => {
      const prev = els.current.get(key);
      if (prev) io.current?.unobserve(prev);
      if (el) {
        els.current.set(key, el);
        io.current?.observe(el);
      } else {
        els.current.delete(key);
        visible.current.delete(key);
        flush();
      }
    },
    [],
  );
  useEffect(() => () => io.current?.disconnect(), []);

  if (error && !cfg) return <ErrorNote error={`Could not load the configuration: ${error}`} retry={reload} />;
  if (!cfg) return <Loading what="the configuration" />;

  const sats = cfg.satellites;
  const tps = [...new Set(sats.flatMap((s) => s.transponders.map((t) => t.id)))];
  return (
    <div className="spectrum-view">
      <div className="toolbar" role="group" aria-label="Spectrum filters">
        <label>
          Site{' '}
          <select value={site} onChange={(e) => setParams({ site: e.target.value || null })}>
            <option value="">All sites</option>
            {cfg.sites.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Satellite{' '}
          <select value={sat} onChange={(e) => setParams({ sat: e.target.value || null })}>
            <option value="">All satellites</option>
            {sats.map((s) => (
              <option key={s.id} value={s.id}>
                {s.id}
              </option>
            ))}
          </select>
        </label>
        <label>
          Transponder{' '}
          <select value={tp} onChange={(e) => setParams({ tp: e.target.value || null })}>
            <option value="">All transponders</option>
            {tps.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </label>
        <span className="seg" role="group" aria-label="Columns">
          {[1, 2, 3, 4].map((n) => (
            <button key={n} type="button" aria-pressed={cols === n} onClick={() => setParams({ cols: String(n) })}>
              {n} col
            </button>
          ))}
        </span>
        <label>
          <input type="checkbox" checked={dense} onChange={(e) => setParams({ dense: e.target.checked ? '1' : null })} /> Compact
        </label>
        <span className="spacer" />
        <span className="ink2">
          {shown.length} of {windows.length} windows; level in dB, thermal noise = 0 dB
        </span>
      </div>
      {shown.length === 0 ? (
        <Empty title="No window matches these filters" action={<button type="button" className="btn" onClick={() => setParams({ site: null, sat: null, tp: null })}>Clear filters</button>}>
          The selected site does not watch the selected satellite or transponder.
        </Empty>
      ) : (
        <div className={`specgrid${dense ? ' dense' : ''}`} style={{ gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))` }}>
          {shown.map((w) => (
            <Panel key={w.key} w={w} engine={engine} offline={offline.has(w.site.id)} observe={observe} />
          ))}
        </div>
      )}
    </div>
  );
}
