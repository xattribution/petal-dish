// Console shell: marking banner (from /meta, top and bottom), tab row, roll-up status,
// link state, theme, token prompt, clock strip; the active view below.
import { lazy, Suspense, useEffect, useMemo, useRef, useState } from 'react';
import type { Marking, Site } from './api/types.gen';
import { Loading, Status, StatusSymbol, useTick } from './components/common';
import { compareEvents, eventLevel, isActive, nodeLevel, worst, type Level } from './domain/status';
import { countryAt, onWorld, worldIfLoaded } from './map/world';
import { href, navigate, useRoute, type ViewName } from './router';
import { configResource } from './state/resources';
import { getState, hasToken, serverNowMs, setToken, useLive } from './state/store';
import { setTheme, useTheme, type ThemeSetting } from './styles/theme';
import { formatAge, zoneClock, zuluClock, dtg } from './time/dtg';
import { siteZone } from './time/zones';
import { WatchView } from './views/Watch';

const SpectrumView = lazy(() => import('./spectrum/SpectrumView').then((m) => ({ default: m.SpectrumView })));
const NetworkView = lazy(() => import('./views/Network').then((m) => ({ default: m.NetworkView })));
const PlanView = lazy(() => import('./views/Plan').then((m) => ({ default: m.PlanView })));
const ConfigureView = lazy(() => import('./views/Configure').then((m) => ({ default: m.ConfigureView })));
const SimulationView = lazy(() => import('./views/Simulation').then((m) => ({ default: m.SimulationView })));

function levelKey(m: Marking): string {
  const l = m.level.toLowerCase().replace(/[^a-z]/g, '');
  if (l.startsWith('unclass')) return 'unclassified';
  if (l === 'cui') return 'cui';
  if (l.startsWith('confid')) return 'confidential';
  if (l === 'topsecretsci' || l === 'tssci') return 'ts-sci';
  if (l.startsWith('topsecret') || l === 'ts') return 'top-secret';
  if (l.startsWith('secret')) return 'secret';
  return 'other';
}

function Banner({ marking, where }: { marking: Marking | null | undefined; where: 'top' | 'bottom' }) {
  if (!marking) return null;
  return (
    <div className="banner" data-level={levelKey(marking)} role={where === 'top' ? 'banner' : undefined} aria-label={`Marking: ${marking.line}`}>
      {marking.line}
    </div>
  );
}

const TABS: Array<{ view: ViewName; label: string; key: string }> = [
  { view: 'watch', label: 'Watch', key: '1' },
  { view: 'spectrum', label: 'Spectrum', key: '2' },
  { view: 'network', label: 'Network', key: '3' },
  { view: 'plan', label: 'Plan', key: '4' },
  { view: 'configure', label: 'Configure', key: '5' },
  { view: 'simulation', label: 'Simulation', key: '6' },
];

function LinkState() {
  const link = useLive((s) => s.link);
  const statusAt = useLive((s) => s.status?.receivedAt ?? null);
  const now = useTick(500);
  if (link.kind === 'reconnecting') {
    const secs = Math.max(0, Math.ceil((link.retryAt - now) / 1000));
    return (
      <span className="linkstate" title={link.reason}>
        <StatusSymbol level="serious" />
        <span>
          Disconnected: {link.reason}. Retry in {secs} s
        </span>
      </span>
    );
  }
  if (link.kind === 'connecting') {
    return (
      <span className="linkstate">
        <StatusSymbol level="standby" />
        <span>Connecting{link.attempt > 1 ? ` (attempt ${link.attempt})` : ''}</span>
      </span>
    );
  }
  if (link.kind === 'live') {
    const age = statusAt === null ? Infinity : (now - statusAt) / 1000;
    if (age > 3) {
      return (
        <span className="linkstate">
          <StatusSymbol level="caution" />
          <span>Live, but no status for {formatAge(age)}</span>
        </span>
      );
    }
    return (
      <span className="linkstate">
        <StatusSymbol level="normal" />
        <span>Live</span>
      </span>
    );
  }
  return (
    <span className="linkstate">
      <StatusSymbol level="off" />
      <span>Not connected</span>
    </span>
  );
}

function RollupBadge() {
  const nodes = useLive((s) => s.status?.nodes ?? null);
  const summaries = useLive((s) => s.summaries);
  const level: Level = useMemo(() => {
    const ls: Level[] = [];
    for (const n of nodes ?? []) ls.push(nodeLevel(n).level);
    for (const e of summaries.values()) if (isActive(e)) ls.push(eventLevel(e));
    return worst(ls, 'normal');
  }, [nodes, summaries]);
  const top = useMemo(() => [...summaries.values()].filter(isActive).sort(compareEvents)[0], [summaries]);
  return (
    <a className="linkstate" href={href('watch')} style={{ textDecoration: 'none' }} aria-label="Overall status, open the watch screen">
      <Status level={level} />
      <span className="ink2">{top ? `${top.id} ${top.verdict}` : 'no active events'}</span>
    </a>
  );
}

function ThemeSwitch() {
  const { setting } = useTheme();
  const opts: Array<[ThemeSetting, string]> = [
    ['auto', 'Auto'],
    ['dark', 'Dark'],
    ['light', 'Light'],
  ];
  return (
    <span className="seg" role="group" aria-label="Theme">
      {opts.map(([v, l]) => (
        <button key={v} type="button" aria-pressed={setting === v} onClick={() => setTheme(v)}>
          {l}
        </button>
      ))}
    </span>
  );
}

function useSiteZones(sites: Site[] | undefined): Map<string, string> {
  const [ready, setReady] = useState(0);
  useEffect(() => onWorld(() => setReady((n) => n + 1)), []);
  return useMemo(() => {
    const m = new Map<string, string>();
    const world = worldIfLoaded();
    for (const s of sites ?? []) {
      const country = world ? countryAt(world, s.lat_deg, s.lon_deg) : null;
      m.set(s.id, siteZone(s.lat_deg, s.lon_deg, country));
    }
    return m;
  }, [sites, ready]);
}

function ClockStrip() {
  useTick(1000);
  const { data } = configResource.use();
  const sites = data?.envelope.config.sites;
  const zones = useSiteZones(sites);
  const now = new Date(serverNowMs());
  const offset = useLive((s) => s.serverOffsetMs);
  // sites that show the same local time share one entry: "Tokyo, Naha, Seoul 2058I +09:00"
  const groups: Array<{ key: string; names: string[]; zones: string; c: ReturnType<typeof zoneClock> }> = [];
  for (const s of sites ?? []) {
    const tz = zones.get(s.id) ?? 'UTC';
    const c = zoneClock(now, tz);
    const key = `${c.hhmm}${c.letter ?? ''}${c.offset}${c.day}`;
    const name = s.name.replace(/\s*\(.*\)$/, '');
    const g = groups.find((x) => x.key === key);
    if (g) {
      g.names.push(name);
      if (!g.zones.includes(tz)) g.zones += `, ${tz}`;
    } else groups.push({ key, names: [name], zones: tz, c });
  }
  return (
    <div className="clockstrip" aria-label="Clocks">
      <span className="zulu mono" aria-label="Zulu time">
        {dtg(now)} <span className="ink2">{zuluClock(now)}</span>
      </span>
      {Math.abs(offset) > 2000 ? <span className="err-text">console clock differs from the server by {formatAge(Math.abs(offset) / 1000)}; times shown are the server's</span> : null}
      {groups.map((g) => (
        <span className="site" key={g.key} title={g.zones}>
          {g.names.join(', ')} <b className="mono">{g.c.hhmm}{g.c.letter ?? ''}</b> <span className="mono off">{g.c.offset}</span>
          {g.c.day ? <span className="day mono"> {g.c.day}</span> : null}
        </span>
      ))}
    </div>
  );
}

function TokenDialog() {
  const need = useLive((s) => s.needToken);
  const ref = useRef<HTMLDialogElement>(null);
  const [value, setValue] = useState('');
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (need && !d.open) d.showModal();
    if (!need && d.open) d.close();
  }, [need]);
  return (
    <dialog ref={ref} aria-labelledby="token-title" onCancel={(e) => e.preventDefault()}>
      <form
        method="dialog"
        onSubmit={(e) => {
          e.preventDefault();
          setToken(value);
        }}
      >
        <h2 id="token-title">API token required</h2>
        <p>{hasToken() ? 'The server refused the token. ' : ''}This server only answers requests that carry its API token. The token is kept for this browser tab only.</p>
        <div className="field">
          <label htmlFor="token-input">Token</label>
          <input id="token-input" type="password" autoComplete="off" value={value} onChange={(e) => setValue(e.target.value)} required />
        </div>
        <p />
        <button type="submit" className="btn primary">
          Use token
        </button>
      </form>
    </dialog>
  );
}

export function App() {
  const route = useRoute();
  const meta = useLive((s) => s.meta);
  const metaError = useLive((s) => s.metaError);
  const simulation = meta?.simulation === true;

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (e.ctrlKey || e.metaKey || e.altKey || /^(INPUT|SELECT|TEXTAREA)$/.test(t.tagName) || t.isContentEditable) return;
      const tab = TABS.find((x) => x.key === e.key);
      if (tab && (tab.view !== 'simulation' || getState().meta?.simulation)) {
        e.preventDefault();
        navigate(href(tab.view));
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const current = route.view === 'event' ? 'watch' : route.view;
  let body;
  switch (route.view) {
    case 'watch':
    case 'event':
      body = <WatchView eventId={route.view === 'event' ? route.id : null} />;
      break;
    case 'spectrum':
      body = <SpectrumView />;
      break;
    case 'network':
      body = <NetworkView />;
      break;
    case 'plan':
      body = <PlanView />;
      break;
    case 'configure':
      body = <ConfigureView />;
      break;
    case 'simulation':
      body = <SimulationView />;
      break;
  }
  return (
    <div className="app">
      <Banner marking={meta?.marking} where="top" />
      <header className="appbar">
        <span className="product">PETAL Locate</span>
        {simulation ? (
          <span className="simtag" style={{ alignSelf: 'center' }} title="The server runs a simulated world: every site, signal and event on this console is simulated">
            SIMULATION
          </span>
        ) : null}
        <nav className="tabs" aria-label="Views">
          {TABS.filter((t) => t.view !== 'simulation' || simulation).map((t) => (
            <a key={t.view} className="tab" href={href(t.view)} aria-current={current === t.view ? 'page' : undefined}>
              {t.label}
              <kbd>{t.key}</kbd>
            </a>
          ))}
        </nav>
        <span className="spacer" />
        <div className="appbar-right">
          {metaError ? <span className="err-text">Server: {metaError}</span> : null}
          <RollupBadge />
          <LinkState />
          <ThemeSwitch />
        </div>
      </header>
      <ClockStrip />
      <main>
        <Suspense fallback={<Loading what="the screen" />}>{body}</Suspense>
      </main>
      <Banner marking={meta?.marking} where="bottom" />
      <TokenDialog />
    </div>
  );
}

