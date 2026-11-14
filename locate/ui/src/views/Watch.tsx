// Watch: the first screen. Left rail: network roll-up and the event queue, most urgent
// first. Right: the world map, or the selected event's dossier (#/event/<id>).
import { useEffect, useMemo, useState } from 'react';
import type { EventSummary, SystemConfig } from '../api/types.gen';
import { Empty, ErrorNote, Loading, SimTag, Status, StatusSymbol, useTick } from '../components/common';
import { isInconsistent, isNotFix, semiAxes, summaryCandidatesText, summaryReceivedText, widthText } from '../domain/event';
import { compareEvents, eventLevel, isActive, nodeLevel, satelliteProblems, STATE_WORD, VERDICT_WORD, worst } from '../domain/status';
import { formatLat, formatLon } from '../format/geo';
import { mhz, plural } from '../format/units';
import { MapView, type LayerId, type MapData, type MapSolution } from '../map/MapView';
import { href, navigate } from '../router';
import { configResource, footprintsResource, subpointsResource, truthResource } from '../state/resources';
import { freshness, refreshEvents, serverNowMs, useLive } from '../state/store';
import { dtgNs, formatAge, zuluClock } from '../time/dtg';
import { nsToMs } from '../time/ns';
import { EventDossier } from './EventDossier';

export function useLayers(key: string, defaults: Partial<Record<LayerId, boolean>>): [Partial<Record<LayerId, boolean>>, (l: Partial<Record<LayerId, boolean>>) => void] {
  const [layers, setLayers] = useState<Partial<Record<LayerId, boolean>>>(() => {
    try {
      const raw = localStorage.getItem(`petal.layers.${key}`);
      if (raw) {
        const saved = JSON.parse(raw) as Partial<Record<LayerId, boolean>>;
        const out: Partial<Record<LayerId, boolean>> = {};
        for (const k of Object.keys(defaults) as LayerId[]) out[k] = typeof saved[k] === 'boolean' ? saved[k] : defaults[k];
        return out;
      }
    } catch {
      /* storage blocked */
    }
    return defaults;
  });
  const set = (l: Partial<Record<LayerId, boolean>>) => {
    setLayers(l);
    try {
      localStorage.setItem(`petal.layers.${key}`, JSON.stringify(l));
    } catch {
      /* storage blocked */
    }
  };
  return [layers, set];
}

function Rollup() {
  const status = useLive((s) => s.status);
  const now = useTick(1000);
  if (!status) return <div className="rollup ink2">Waiting for the first status report.</div>;
  const clocks = new Map((status.site_clocks ?? []).map((c) => [c.site_id, c]));
  const assessed = status.nodes.map((n) => ({ n, a: nodeLevel(n, { siteClock: clocks.get(n.site_id) }) }));
  const online = status.nodes.filter((n) => n.online).length;
  const bad = assessed.filter((x) => x.a.level !== 'normal').sort((a, b) => (a.a.level === 'serious' ? -1 : 1) - (b.a.level === 'serious' ? -1 : 1));
  const satsDown = satelliteProblems(status.satellites);
  const level = worst([...assessed.map((x) => x.a.level), ...(satsDown.length ? (['serious'] as const) : [])]);
  const age = (now - status.receivedAt) / 1000;
  return (
    <div className="rollup" aria-label="Network roll-up">
      <div className="line">
        <Status level={level} />
        <a href={href('network')}>
          {online} of {status.nodes.length} nodes online
        </a>
        {age > 3 ? <span className="err-text">status {formatAge(age)} old</span> : null}
      </div>
      {satsDown.map((s) => (
        // ADAPT-7: a satellite the server cannot place is named here, not only in the log
        <div className="line ink2 satdown" key={s.sat_id}>
          <StatusSymbol level="serious" />
          <span className="wrap">{s.message ?? `satellite ${s.sat_id}: ephemeris not available`}</span>
        </div>
      ))}
      {bad.map(({ n, a }) => (
        <div className="line ink2" key={n.site_id}>
          <StatusSymbol level={a.level} />
          <span className="mono">{n.site_id}</span>
          <span>{a.reasons[0]}</span>
        </div>
      ))}
      <div className="ink2" style={{ fontSize: 12 }}>
        Server: {plural(status.queue.captures_pending, 'capture')} pending, {plural(status.queue.jobs_running, 'job')} running, {status.queue.jobs_waiting} waiting
      </div>
    </div>
  );
}

/** Waveform class words (ClassificationOut.label). */
const CLASS_WORD: Record<string, string> = { cw: 'CW carrier', swept: 'swept', noise: 'noise', modulated: 'modulated', pulsed: 'pulsed', unknown: 'unknown waveform' };

function QueueRow({ e, current, nowMs, live, cfg }: { e: EventSummary; current: boolean; nowMs: number; live: boolean; cfg: SystemConfig | null }) {
  const level = eventLevel(e);
  const age = (nowMs - nsToMs(e.last_seen_ns)) / 1000;
  const sol = e.solution;
  // UX-8: "now" only while the console is live; otherwise how long ago the last update saw it
  const ageText = isActive(e) ? (live && age < 15 ? 'now' : `${formatAge(age)} ago`) : dtgNs(e.last_seen_ns);
  // UX-3: a local signal is described by what is received at the site, not an uplink equivalent
  const local = e.verdict === 'local' ? summaryReceivedText(e, cfg) : null;
  const amb = sol ? summaryCandidatesText(sol) : null;
  // ADAPT-5: every detection fills its window, so the range shown is a lower bound
  const [wLo, wHi] = local ? [e.received_lo_hz, e.received_hi_hz] : [e.uplink_lo_hz, e.uplink_hi_hz];
  const wider = e.width_lower_bound && wLo !== null && wLo !== undefined && wHi !== null && wHi !== undefined ? `, ${widthText(wLo, wHi, true)} wide` : '';
  return (
    <a className="qrow" href={href('event', e.id)} aria-current={current ? 'true' : undefined} data-event={e.id}>
      <span className="sym">
        <StatusSymbol level={level} size={13} />
      </span>
      <span className="l1">
        <span className="id mono">{e.id}</span>
        <span className="status">
          <span className="word">{VERDICT_WORD[e.verdict]}</span>
        </span>
        <span className="ink2">{STATE_WORD[e.state]}</span>
        {e.simulated ? <SimTag text="SIM" /> : null}
      </span>
      <span className="age mono">{ageText}</span>
      <span className="l2">
        {local ? (
          <span className="mono">{local}</span>
        ) : (
          <span className="mono">{e.uplink_lo_hz !== null && e.uplink_hi_hz !== null ? `${mhz(e.uplink_lo_hz, 4)}–${mhz(e.uplink_hi_hz, 4)} MHz up` : 'outside every transponder'}</span>
        )}
        {wider}
        {e.label ? `, ${CLASS_WORD[e.label] ?? e.label}` : ''}
        {local ? null : (
          <>
            <br />
            <span className="mono">{e.satellite_ids.join('  ')}</span>
          </>
        )}
      </span>
      <span className="l3">
        {sol && isInconsistent(sol) ? (
          // UX-1: an inconsistent fit is a caution, never a confident position
          <span className="status amb">
            <StatusSymbol level="caution" /> Measurements inconsistent: no fix
          </span>
        ) : sol && isNotFix(sol) ? (
          // R4: no redundancy yet, or an ellipse too large to pin the position down
          <span className="status amb">
            <StatusSymbol level="caution" /> Not a fix yet: more measurements needed
          </span>
        ) : sol && amb ? (
          <span className="amb">Ambiguous, {amb}</span>
        ) : sol ? (
          <>
            <span className="mono nowrap">
              {formatLat(sol.lat_deg, 2)} {formatLon(sol.lon_deg, 2)}
            </span>{' '}
            <span className="nowrap">95 % semi-axes {semiAxes(sol.semi_major_m, sol.semi_minor_m)}</span>
          </>
        ) : (
          e.headline
        )}
      </span>
    </a>
  );
}

function Queue({ selected }: { selected: string | null }) {
  const summaries = useLive((s) => s.summaries);
  const loaded = useLive((s) => s.eventsLoaded);
  const error = useLive((s) => s.eventsError);
  const link = useLive((s) => s.link);
  const status = useLive((s) => s.status);
  const cfg = configResource.use().data?.envelope.config ?? null;
  // ages advance every second, and the rows know whether the console is live
  useTick(1000);
  const { live, asOfMs } = freshness({ link, status }, Date.now());
  const nowMs = serverNowMs();
  const [showInactive, setShowInactive] = useState(false);
  const sorted = useMemo(() => [...summaries.values()].sort(compareEvents), [summaries]);
  const active = sorted.filter(isActive);
  const inactive = sorted.filter((e) => !isActive(e));

  useEffect(() => {
    const onKey = (ev: KeyboardEvent) => {
      const t = ev.target as HTMLElement;
      if (/^(INPUT|SELECT|TEXTAREA)$/.test(t.tagName) || ev.ctrlKey || ev.metaKey || ev.altKey) return;
      const list = showInactive ? sorted : active;
      if (!list.length) return;
      const i = list.findIndex((e) => e.id === selected);
      if (ev.key === 'j' || ev.key === 'k') {
        ev.preventDefault();
        const next = ev.key === 'j' ? Math.min(list.length - 1, i + 1) : Math.max(0, i < 0 ? 0 : i - 1);
        const target = list[next];
        if (target) navigate(href('event', target.id));
      } else if (ev.key === 'Escape' && selected) {
        navigate(href('watch'));
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [sorted, active, selected, showInactive]);

  useEffect(() => {
    if (selected) document.querySelector(`[data-event="${CSS.escape(selected)}"]`)?.scrollIntoView({ block: 'nearest' });
  }, [selected]);

  if (!loaded) return <Loading what="events" />;
  return (
    <nav className="queue" aria-label="Interference events">
      {error ? <ErrorNote error={`Events could not be read: ${error}`} retry={() => void refreshEvents()} /> : null}
      <h2>
        <span>Events: {active.length} active</span>
        <span className="muted" style={{ fontWeight: 400 }}>
          {live ? 'j / k to move' : `as of ${asOfMs === null ? 'no update' : zuluClock(new Date(asOfMs + (nowMs - Date.now())))}`}
        </span>
      </h2>
      {active.length === 0 ? (
        <Empty title="No active interference">Every monitored window is clear. New detections appear here, most urgent first.</Empty>
      ) : (
        active.map((e) => <QueueRow key={e.id} e={e} current={e.id === selected} nowMs={nowMs} live={live} cfg={cfg} />)
      )}
      {inactive.length ? (
        <>
          <h2>
            <button type="button" className="linkbtn" aria-expanded={showInactive} onClick={() => setShowInactive(!showInactive)}>
              {showInactive ? 'Hide' : 'Show'} {inactive.length} lost or closed
            </button>
          </h2>
          {showInactive ? inactive.map((e) => <QueueRow key={e.id} e={e} current={e.id === selected} nowMs={nowMs} live={live} cfg={cfg} />) : null}
        </>
      ) : null}
    </nav>
  );
}

function OverviewMap() {
  const cfg = configResource.use();
  const subs = subpointsResource.use();
  const fps = footprintsResource(0).use();
  const simulation = useLive((s) => s.meta?.simulation === true);
  const summaries = useLive((s) => s.summaries);
  const truth = truthResource.use(simulation);
  const [layers, setLayers] = useLayers('watch', { sites: true, satellites: true, footprints: false, events: true, ...(simulation ? { truth: false } : {}) });
  const data: MapData = useMemo(() => {
    const solutions: MapSolution[] = [];
    for (const e of summaries.values()) {
      if (!isActive(e) || !e.solution) continue;
      const s = e.solution;
      const bad = isNotFix(s);
      solutions.push({
        id: e.id,
        eventId: e.id,
        lat: s.lat_deg,
        lon: s.lon_deg,
        semiMajorM: s.semi_major_m,
        semiMinorM: s.semi_minor_m,
        azimuthDeg: s.azimuth_deg,
        role: bad ? 'unreliable' : 'fix',
        selected: false,
        label: `${e.id}${bad ? ' (not a fix)' : s.ambiguous ? ' (ambiguous)' : ''}`,
      });
      // an unresolved ambiguity: every other position that fits as well, dashed
      (s.ambiguities ?? []).forEach((a, i) =>
        solutions.push({ id: `${e.id}-mirror-${i}`, eventId: e.id, lat: a.lat_deg, lon: a.lon_deg, semiMajorM: a.semi_major_m, semiMinorM: a.semi_minor_m, azimuthDeg: a.azimuth_deg, role: 'mirror', selected: false, label: `${e.id} position ${i + 2}` }),
      );
    }
    return {
      sites: cfg.data?.envelope.config.sites.map((s) => ({ id: s.id, name: s.name, lat: s.lat_deg, lon: s.lon_deg })) ?? [],
      subpoints: subs.data ?? [],
      footprints: fps.data ?? [],
      solutions,
      truth: simulation ? (truth.data?.emitters ?? []).filter((t) => t.kind !== 'carrier').map((t) => ({ id: t.id, lat: t.lat_deg, lon: t.lon_deg, label: `${t.id} (SIMULATION)`, enabled: t.enabled })) : [],
    };
  }, [cfg.data, subs.data, fps.data, summaries, truth.data, simulation]);
  return (
    <MapView
      label="World map of sites, satellites and located emitters"
      data={data}
      layers={simulation ? layers : { ...layers, truth: undefined }}
      onLayers={setLayers}
      region={cfg.data?.envelope.config.processing?.region ?? null}
      onPick={(kind, id) => {
        if (kind === 'solution') navigate(href('event', id));
        if (kind === 'site') navigate(href('network', null, { site: id }));
      }}
      truthLabel="Simulation truth (SIMULATION)"
    />
  );
}

export function WatchView({ eventId }: { eventId: string | null }) {
  return (
    <div className="watch">
      <aside className="rail">
        <Rollup />
        <Queue selected={eventId} />
      </aside>
      {eventId ? <EventDossier key={eventId} id={eventId} /> : <OverviewMap />}
    </div>
  );
}
