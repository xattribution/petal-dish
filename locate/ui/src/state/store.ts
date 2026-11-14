// Live console state: meta, status, events, link state. A small external store read with
// useSyncExternalStore; spectrum frames bypass it entirely (spectrum/engine.ts).
import { useSyncExternalStore } from 'react';
import { ApiClient, AuthError, type ApiError } from '../api/client';
import type { PsdFrame } from '../api/plb1';
import type { Event, EventSummary, Meta, NodeStatus, QueueStatus, SatelliteStatus, SiteClockEstimate } from '../api/types.gen';
import { UiSocket, socketUrl, type LinkState, type WsMessage } from '../api/ws';
import { nsCompare, nsToMs } from '../time/ns';

export interface StatusSnapshot {
  server_t_ns: string;
  config_version: number;
  nodes: NodeStatus[];
  events_active: number;
  queue: QueueStatus;
  /** whether the server can place each satellite now (Status.satellites) */
  satellites: SatelliteStatus[];
  /** site clock offsets estimated from reference calibrations (Status.site_clocks) */
  site_clocks: SiteClockEstimate[];
  /** client clock (ms) when it arrived */
  receivedAt: number;
}

export interface LiveState {
  meta: Meta | null;
  metaError: string | null;
  needToken: boolean;
  status: StatusSnapshot | null;
  link: LinkState;
  /** server clock minus client clock, ms, from the newest status */
  serverOffsetMs: number;
  summaries: Map<string, EventSummary>;
  events: Map<string, Event>;
  eventsLoaded: boolean;
  eventsError: string | null;
  configVersion: number | null;
  simVersion: number | null;
  protocolErrors: number;
}

const TOKEN_KEY = 'petal.apiToken';

function readToken(): string | null {
  try {
    return sessionStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

let state: LiveState = {
  meta: null,
  metaError: null,
  needToken: false,
  status: null,
  link: { kind: 'stopped' },
  serverOffsetMs: 0,
  summaries: new Map(),
  events: new Map(),
  eventsLoaded: false,
  eventsError: null,
  configVersion: null,
  simVersion: null,
  protocolErrors: 0,
};

const listeners = new Set<() => void>();
function set(patch: Partial<LiveState>): void {
  state = { ...state, ...patch };
  for (const l of listeners) l();
}
export function getState(): LiveState {
  return state;
}
export function subscribe(l: () => void): () => void {
  listeners.add(l);
  return () => listeners.delete(l);
}
export function useLive<T>(select: (s: LiveState) => T): T {
  return useSyncExternalStore(subscribe, () => select(state), () => select(state));
}

let token: string | null = readToken();
export const api = new ApiClient({ getToken: () => token });

export function setToken(value: string | null): void {
  token = value && value.trim() ? value.trim() : null;
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    /* private mode: the token lives for this page only */
  }
  set({ needToken: false });
  void boot();
  socket.restart();
}
export function hasToken(): boolean {
  return token !== null;
}

// ------------------------------------------------------------------ spectrum fan-out
type FrameListener = (f: PsdFrame) => void;
const frameListeners = new Set<FrameListener>();
export function onFrame(l: FrameListener): () => void {
  frameListeners.add(l);
  return () => frameListeners.delete(l);
}

// ------------------------------------------------------------------ events
export function summaryOf(e: Event): EventSummary {
  const s = e.solution;
  return {
    id: e.id,
    state: e.state,
    verdict: e.verdict,
    first_seen_ns: e.first_seen_ns,
    last_seen_ns: e.last_seen_ns,
    uplink_lo_hz: e.uplink_lo_hz,
    uplink_hi_hz: e.uplink_hi_hz,
    satellite_ids: e.satellite_ids,
    label: e.classification?.label ?? null,
    headline: e.advisory.headline,
    solution: s
      ? {
          lat_deg: s.lat_deg,
          lon_deg: s.lon_deg,
          semi_major_m: s.ellipse.semi_major_m,
          semi_minor_m: s.ellipse.semi_minor_m,
          azimuth_deg: s.ellipse.azimuth_deg,
          cep50_m: s.cep50_m,
          ambiguous: s.ambiguities.length > 0,
          ambiguities: s.ambiguities.map((a) => ({ lat_deg: a.lat_deg, lon_deg: a.lon_deg, semi_major_m: a.ellipse.semi_major_m, semi_minor_m: a.ellipse.semi_minor_m, azimuth_deg: a.ellipse.azimuth_deg })),
          fit_consistent: s.fit_consistent ?? true,
          is_fix: s.is_fix ?? true,
        }
      : null,
    simulated: e.simulated ?? false,
    onset_ns: e.onset_ns ?? null,
    received_lo_hz: e.received_lo_hz ?? null,
    received_hi_hz: e.received_hi_hz ?? null,
    seen_site_ids: e.seen_site_ids ?? [],
    width_lower_bound: e.width_lower_bound ?? false,
  };
}

function putEvent(e: Event): void {
  const events = new Map(state.events);
  events.set(e.id, e);
  const summaries = new Map(state.summaries);
  summaries.set(e.id, summaryOf(e));
  set({ events, summaries });
}

export async function loadEvent(id: string): Promise<Event> {
  const e = await api.event(id);
  putEvent(e);
  return e;
}

export async function refreshEvents(): Promise<void> {
  try {
    const list = await api.events({ limit: 500 });
    const summaries = new Map<string, EventSummary>();
    for (const s of list) summaries.set(s.id, s);
    set({ summaries, eventsLoaded: true, eventsError: null });
  } catch (e) {
    handleError(e as ApiError);
    set({ eventsError: (e as Error).message, eventsLoaded: true });
  }
}

function handleError(e: ApiError): void {
  if (e instanceof AuthError) set({ needToken: true });
}

// ------------------------------------------------------------------ socket
function onMessage(msg: WsMessage): void {
  switch (msg.type) {
    case 'status': {
      const receivedAt = Date.now();
      set({
        status: {
          server_t_ns: msg.server_t_ns,
          config_version: msg.config_version,
          nodes: msg.nodes,
          events_active: msg.events_active,
          queue: msg.queue,
          satellites: msg.satellites ?? [],
          site_clocks: msg.site_clocks ?? [],
          receivedAt,
        },
        serverOffsetMs: nsToMs(msg.server_t_ns) - receivedAt,
        configVersion: msg.config_version,
        meta: state.meta ? { ...state.meta, marking: msg.marking, simulation: msg.simulation } : state.meta,
      });
      break;
    }
    case 'event': {
      const prev = state.events.get(msg.event.id);
      if (!prev || nsCompare(msg.event.last_seen_ns, prev.last_seen_ns) >= 0 || msg.event.state !== prev.state) putEvent(msg.event);
      break;
    }
    case 'config':
      set({ configVersion: msg.config_version });
      break;
    case 'sim':
      set({ simVersion: msg.version });
      break;
  }
}

export const socket = new UiSocket({
  url: () => socketUrl(window.location, token),
  handlers: {
    onMessage,
    onFrame: (f) => {
      for (const l of frameListeners) l(f);
    },
    onState: (link) => {
      const wasLive = state.link.kind === 'live';
      if (link.kind === 'reconnecting' && link.authRefused) {
        // close code 1008: retrying with the same token cannot work; ask for one (setToken restarts the socket)
        set({ link, needToken: true });
        queueMicrotask(() => socket.stop());
        return;
      }
      set({ link });
      // after a reconnect, re-read what may have changed while no messages arrived
      if (link.kind === 'live' && !wasLive && booted) {
        void refreshEvents();
        for (const [id, e] of state.events) if (e.state !== 'closed') void loadEvent(id).catch(() => undefined);
      }
    },
    onProtocolError: () => set({ protocolErrors: state.protocolErrors + 1 }),
  },
});

let booted = false;
export async function boot(): Promise<void> {
  try {
    const meta = await api.meta();
    set({ meta, metaError: null, needToken: meta.api_token_required === true && !token });
    if (state.needToken) return;
  } catch (e) {
    handleError(e as ApiError);
    set({ metaError: (e as Error).message });
  }
  await refreshEvents();
  try {
    const st = await api.status();
    if (!state.status) {
      const receivedAt = Date.now();
      set({ status: { ...st, satellites: st.satellites ?? [], site_clocks: st.site_clocks ?? [], receivedAt }, serverOffsetMs: nsToMs(st.server_t_ns) - receivedAt, configVersion: st.config_version });
    }
  } catch (e) {
    handleError(e as ApiError);
  }
  if (!booted) {
    booted = true;
    socket.start();
    // the event list is also re-read now and then, in case a message was missed while reconnecting
    setInterval(() => {
      if (state.link.kind === 'live') void refreshEvents();
    }, 30000);
  }
}

/** Server-corrected now, ms since the epoch. */
export function serverNowMs(): number {
  return Date.now() + state.serverOffsetMs;
}

/** Seconds since the newest status message, or Infinity. */
export function statusAge(s: LiveState, nowMs: number): number {
  return s.status ? (nowMs - s.status.receivedAt) / 1000 : Infinity;
}

/** A status report older than this (s) no longer describes the present. */
export const STATUS_STALE_S = 5;

/**
 * Whether what the console shows is current: the socket is live and the newest status
 * is recent. While it is not, live wording ("now", "capturing IQ now") gives way to
 * "as of" wording, and asOfMs (client clock of the newest status, or null) dates it.
 */
export function freshness(s: Pick<LiveState, 'link' | 'status'>, nowMs: number): { live: boolean; asOfMs: number | null } {
  const asOfMs = s.status ? s.status.receivedAt : null;
  const live = s.link.kind === 'live' && asOfMs !== null && (nowMs - asOfMs) / 1000 <= STATUS_STALE_S;
  return { live, asOfMs };
}
