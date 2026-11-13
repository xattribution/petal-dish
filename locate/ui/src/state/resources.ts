// Shared read-only resources: the configuration (refetched when the server announces a
// new version), geometry (sub-points, footprints, look angles) and the simulation truth.
import { useEffect, useSyncExternalStore } from 'react';
import type { ConfigEnvelope, Footprint, LookAngle, SimTruth, Subpoint } from '../api/types.gen';
import { api, getState, subscribe as subscribeLive } from './store';

interface Resource<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  at: number;
}

const all: Array<{ retryIfFailed: () => Promise<void> }> = [];

function createResource<T>(fetcher: () => Promise<T>) {
  let value: Resource<T> = { data: null, error: null, loading: false, at: 0 };
  const ls = new Set<() => void>();
  let inflight: Promise<void> | null = null;
  const set = (v: Partial<Resource<T>>) => {
    value = { ...value, ...v };
    for (const l of ls) l();
  };
  const load = (): Promise<void> => {
    if (inflight) return inflight;
    if (getState().needToken) {
      // nothing is sent without the token; loaded once the operator supplies it
      set({ error: 'waiting for the API token', loading: false });
      return Promise.resolve();
    }
    set({ loading: true });
    inflight = fetcher()
      .then((data) => set({ data, error: null, loading: false, at: Date.now() }))
      .catch((e: Error) => set({ error: e.message, loading: false }))
      .finally(() => {
        inflight = null;
      });
    return inflight;
  };
  const subscribe = (l: () => void) => {
    ls.add(l);
    return () => ls.delete(l);
  };
  all.push({ retryIfFailed: () => (value.error ? load() : Promise.resolve()) });
  return {
    load,
    get: () => value,
    set: (data: T) => set({ data, error: null, at: Date.now() }),
    use(enabled = true): Resource<T> & { reload: () => Promise<void> } {
      const v = useSyncExternalStore(subscribe, () => value, () => value);
      useEffect(() => {
        if (enabled && !value.data && !value.loading && !value.error) void load();
      }, [enabled]);
      return { ...v, reload: load };
    },
  };
}

export const configResource = createResource<{ envelope: ConfigEnvelope; etag: string | null }>(async () => {
  const r = await api.getConfig();
  return { envelope: r.data, etag: r.etag };
});

export const subpointsResource = createResource<Subpoint[]>(() => api.subpoints());
export const lookAnglesResource = createResource<LookAngle[]>(() => api.lookAngles());
const footprintCache = new Map<number, ReturnType<typeof createResource<Footprint[]>>>();
export function footprintsResource(minEl: number) {
  let r = footprintCache.get(minEl);
  if (!r) {
    r = createResource<Footprint[]>(() => api.footprints(minEl));
    footprintCache.set(minEl, r);
  }
  return r;
}
export const truthResource = createResource<SimTruth>(() => api.truth());

// Refetch the configuration when the server announces a new version, and the truth when
// the scenario changes; refresh sub-points every 10 minutes (geosynchronous satellites
// trace a figure-eight over a sidereal day).
let lastConfig: number | null = null;
let lastSim: number | null = null;
let neededToken = false;
let wasLive = false;
subscribeLive(() => {
  const s = getState();
  if (neededToken && !s.needToken) for (const r of all) void r.retryIfFailed();
  neededToken = s.needToken;
  // a load that failed while the server was unreachable is retried when the link is back
  const live = s.link.kind === 'live';
  if (live && !wasLive) for (const r of all) void r.retryIfFailed();
  wasLive = live;
  const cur = configResource.get().data?.envelope.version ?? null;
  if (s.configVersion !== null && s.configVersion !== lastConfig) {
    lastConfig = s.configVersion;
    if (cur !== null && cur !== s.configVersion) void configResource.load();
  }
  if (s.simVersion !== null && s.simVersion !== lastSim) {
    lastSim = s.simVersion;
    if (truthResource.get().data) void truthResource.load();
  }
});
setInterval(() => {
  if (subpointsResource.get().data) void subpointsResource.load();
}, 600000);
