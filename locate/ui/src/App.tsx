// Console shell: marking banner (from /meta, top and bottom), tab row, roll-up status,
// link state, theme, token prompt, clock strip; the active view below.
import { Component, Suspense, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import type { Marking, Site } from './api/types.gen';
import { Loading, Status, StatusSymbol, useTick } from './components/common';
import { compareEvents, isActive, rollup } from './domain/status';
import { retryableLazy } from './lazyView';
import { countryAt, onWorld, worldIfLoaded } from './map/world';
import { href, navigate, useRoute, type ViewName } from './router';
import { configResource } from './state/resources';
import { freshness, getState, hasToken, serverNowMs, setToken, useLive } from './state/store';
import { setTheme, useTheme, type ThemeSetting } from './styles/theme';
import { formatAge, zoneClock, zuluClock, dtg } from './time/dtg';
import { knownZone, siteZone } from './time/zones';
import { WatchView } from './views/Watch';

// Screens other than Watch load on first use. A failed load (server down, network cut)
// stays inside the view area (ViewBoundary) and is retried; they are also fetched ahead
// once the console is up, so an outage later cannot strand a screen.
const VIEWS = {
  spectrum: retryableLazy(() => import('./spectrum/SpectrumView'), (m) => m.SpectrumView),
  network: retryableLazy(() => import('./views/Network'), (m) => m.NetworkView),
  plan: retryableLazy(() => import('./views/Plan'), (m) => m.PlanView),
  configure: retryableLazy(() => import('./views/Configure'), (m) => m.ConfigureView),
  simulation: retryableLazy(() => import('./views/Simulation'), (m) => m.SimulationView),
};

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
    // the countdown first, so a long reason cannot push it out of sight
    return (
      <span className="linkstate" title={`Disconnected: ${link.reason}`}>
        <StatusSymbol level="serious" />
        <span className="countdown">Disconnected, retry in {secs} s:</span>
        <span>{link.reason}</span>
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
  const status = useLive((s) => s.status);
  const link = useLive((s) => s.link);
  const summaries = useLive((s) => s.summaries);
  const now = useTick(1000);
  const live = freshness({ link, status }, now).live;
  const r = useMemo(() => rollup({ status, live, events: summaries.values() }), [status, live, summaries]);
  const top = useMemo(() => [...summaries.values()].filter(isActive).sort(compareEvents)[0], [summaries]);
  // never a green all-clear without data: Off "No data" until the first status, and the
  // last level marked as not current while the link is down or the status is old
  const text = !status
    ? 'no status yet'
    : !r.current
      ? `not current: status ${formatAge((now - status.receivedAt) / 1000)} old`
      : top
        ? `${top.id} ${top.verdict}`
        : 'no active events';
  return (
    <a className="linkstate rollupbadge" href={href('watch')} style={{ textDecoration: 'none' }} aria-label="Overall status, open the watch screen" data-current={r.current ? 'true' : 'false'}>
      <Status level={r.level} word={r.word} />
      <span className="ink2">{text}</span>
    </a>
  );
}

function ThemeSwitch() {
  const { setting } = useTheme();
  return (
    <label className="themesel">
      Theme{' '}
      <select value={setting} onChange={(e) => setTheme(e.target.value as ThemeSetting)}>
        <option value="auto">Auto</option>
        <option value="dark">Dark</option>
        <option value="light">Light</option>
      </select>
    </label>
  );
}

function useSiteZones(sites: Site[] | undefined): Map<string, string> {
  const [ready, setReady] = useState(0);
  useEffect(() => onWorld(() => setReady((n) => n + 1)), []);
  return useMemo(() => {
    const m = new Map<string, string>();
    const world = worldIfLoaded();
    for (const s of sites ?? []) {
      if (knownZone(s.time_zone)) {
        m.set(s.id, s.time_zone); // configured: authoritative
        continue;
      }
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
  const simulated = useLive((s) => s.meta?.simulation === true);
  // sites that show the same local time share one entry: "Tokyo, Naha, Seoul 2058I +09:00"
  const groups: Array<{ key: string; names: string[]; zones: string; c: ReturnType<typeof zoneClock> }> = [];
  for (const s of sites ?? []) {
    const tz = zones.get(s.id) ?? 'UTC';
    const c = zoneClock(now, tz);
    const key = `${c.hhmm}${c.letter ?? ''}${c.offset}${c.day}`;
    const name = s.name.replace(/\s*\(.*\)$/, '');
    const label = `${name}: ${tz}${knownZone(s.time_zone) ? ' (configured)' : ' (inferred from the position)'}`;
    const g = groups.find((x) => x.key === key);
    if (g) {
      g.names.push(name);
      g.zones += `; ${label}`;
    } else groups.push({ key, names: [name], zones: label, c });
  }
  return (
    <div className="clockstrip" aria-label="Clocks">
      <span className="zulu mono" aria-label="Zulu time">
        {dtg(now)} <span className="ink2">{zuluClock(now)}</span>
      </span>
      {Math.abs(offset) > 2000 ? (
        simulated ? (
          // a simulated world may run from a fixed epoch (petal-locate demo --start): not a fault
          <span className="ink2">simulated time, {formatAge(Math.abs(offset) / 1000)} {offset < 0 ? 'behind' : 'ahead of'} this computer</span>
        ) : (
          <span className="err-text">console clock differs from the server by {formatAge(Math.abs(offset) / 1000)}; times shown are the server's</span>
        )
      ) : null}
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

/**
 * Keeps a failed screen inside the view area: a chunk that could not be loaded or a
 * render error shows "could not be loaded" with Retry, while the banners, tabs, roll-up
 * and link state stay up. A new resetKey (another screen, a retry) renders the screen again.
 */
class ViewBoundary extends Component<{ label: string; resetKey: string; live: boolean; onError: (failed: boolean) => void; onRetry: () => void; children: ReactNode }, { error: Error | null }> {
  override state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: unknown) {
    return { error: error instanceof Error ? error : new Error(String(error)) };
  }

  override componentDidCatch() {
    this.props.onError(true);
  }

  override componentDidUpdate(prev: { resetKey: string }) {
    if (prev.resetKey !== this.props.resetKey && this.state.error) {
      this.setState({ error: null });
      this.props.onError(false);
    }
  }

  override render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <div className="empty viewerror" role="alert">
        <h2>The {this.props.label} screen could not be loaded</h2>
        <p className="mono">{error.message}</p>
        {this.props.live ? null : <p>It is loaded again when the console reconnects.</p>}
        <span style={{ display: 'inline-flex', gap: 8 }}>
          <button type="button" className="btn" onClick={this.props.onRetry}>
            Retry
          </button>
          {this.props.live ? (
            <button type="button" className="btn" onClick={() => window.location.reload()}>
              Reload the console
            </button>
          ) : null}
        </span>
      </div>
    );
  }
}

/** Last resort: the shell itself failed. The marking banners still show (from the last /meta). */
export class ShellBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  override state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: unknown) {
    return { error: error instanceof Error ? error : new Error(String(error)) };
  }

  override render() {
    if (!this.state.error) return this.props.children;
    const marking = getState().meta?.marking;
    return (
      <div className="app">
        <Banner marking={marking} where="top" />
        <div />
        <div />
        <main>
          <div className="empty" role="alert">
            <h2>The console stopped on an error</h2>
            <p className="mono">{this.state.error.message}</p>
            <button type="button" className="btn" onClick={() => window.location.reload()}>
              Reload the console
            </button>
          </div>
        </main>
        <Banner marking={marking} where="bottom" />
      </div>
    );
  }
}

const VIEW_LABEL: Record<Exclude<ViewName, 'event'>, string> = { watch: 'Watch', spectrum: 'Spectrum', network: 'Network', plan: 'Plan', configure: 'Configure', simulation: 'Simulation' };

export function App() {
  const route = useRoute();
  const meta = useLive((s) => s.meta);
  const metaError = useLive((s) => s.metaError);
  const linkKind = useLive((s) => s.link.kind);
  const simulation = meta?.simulation === true;
  const mainRef = useRef<HTMLElement>(null);
  // set by a tab click or a number key: the next view change moves focus into the view
  const focusOnChange = useRef(false);
  const [attempt, setAttempt] = useState(0);
  const [viewFailed, setViewFailed] = useState(false);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (e.ctrlKey || e.metaKey || e.altKey || /^(INPUT|SELECT|TEXTAREA)$/.test(t.tagName) || t.isContentEditable) return;
      const tab = TABS.find((x) => x.key === e.key);
      if (tab && (tab.view !== 'simulation' || getState().meta?.simulation)) {
        e.preventDefault();
        focusOnChange.current = true;
        navigate(href(tab.view));
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  // the other screens' chunks, fetched once the console is up
  useEffect(() => {
    const prefetch = () => Object.values(VIEWS).forEach((v) => v.prefetch());
    if (typeof window.requestIdleCallback === 'function') window.requestIdleCallback(prefetch, { timeout: 4000 });
    else setTimeout(prefetch, 1500);
  }, []);

  // a screen that failed to load is loaded again as soon as the link is live again
  useEffect(() => {
    if (linkKind === 'live' && viewFailed) setAttempt((n) => n + 1);
  }, [linkKind]);

  const current = route.view === 'event' ? 'watch' : route.view;
  const label = VIEW_LABEL[current];

  useEffect(() => {
    document.title = `${route.view === 'event' && route.id ? `${route.id} – ` : ''}${label} – PETAL Locate`;
  }, [label, route.view, route.id]);

  // keyboard and screen-reader users land in the new view (not on <body>); moving between
  // events (j / k, the queue) does not change the view and keeps focus where it is
  useEffect(() => {
    if (focusOnChange.current) {
      focusOnChange.current = false;
      mainRef.current?.focus({ preventScroll: true });
    }
  }, [current]);

  let body;
  switch (route.view) {
    case 'watch':
    case 'event':
      body = <WatchView eventId={route.view === 'event' ? route.id : null} />;
      break;
    default: {
      const View = VIEWS[route.view].current();
      body = <View />;
    }
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
            <a
              key={t.view}
              className="tab"
              href={href(t.view)}
              aria-current={current === t.view ? 'page' : undefined}
              title={`${t.label} (key ${t.key})`}
              onClick={() => {
                focusOnChange.current = true;
              }}
            >
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
      <main ref={mainRef} tabIndex={-1} aria-label={label}>
        <ViewBoundary label={label} resetKey={`${current}|${attempt}`} live={linkKind === 'live'} onError={setViewFailed} onRetry={() => setAttempt((n) => n + 1)}>
          <Suspense fallback={<Loading what="the screen" />}>{body}</Suspense>
        </ViewBoundary>
      </main>
      <Banner marking={meta?.marking} where="bottom" />
      <TokenDialog />
    </div>
  );
}

