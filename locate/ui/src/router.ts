// Hash routes: the view, the selected event and filters live in the URL so the back
// button and shared links work. #/watch, #/event/EV-0042, #/spectrum?site=tokyo, ...
import { useSyncExternalStore } from 'react';

export type ViewName = 'watch' | 'event' | 'spectrum' | 'network' | 'plan' | 'configure' | 'simulation';

export interface Route {
  view: ViewName;
  id: string | null;
  params: URLSearchParams;
}

const VIEWS: ViewName[] = ['watch', 'event', 'spectrum', 'network', 'plan', 'configure', 'simulation'];

export function parseHash(hash: string): Route {
  const h = hash.replace(/^#\/?/, '');
  const [path = '', query = ''] = h.split('?');
  const [first = '', ...rest] = path.split('/');
  const view = (VIEWS as string[]).includes(first) ? (first as ViewName) : 'watch';
  const id = rest.length && rest.join('/') ? decodeURIComponent(rest.join('/')) : null;
  return { view, id, params: new URLSearchParams(query) };
}

export function href(view: ViewName, id?: string | null, params?: Record<string, string | null | undefined>): string {
  let h = `#/${view}`;
  if (id) h += `/${encodeURIComponent(id)}`;
  if (params) {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v !== null && v !== undefined && v !== '') q.set(k, v);
    const s = q.toString();
    if (s) h += `?${s}`;
  }
  return h;
}

export function navigate(to: string, replace = false): void {
  if (replace) {
    history.replaceState(null, '', to);
    window.dispatchEvent(new HashChangeEvent('hashchange'));
  } else window.location.hash = to;
}

function subscribe(cb: () => void): () => void {
  window.addEventListener('hashchange', cb);
  return () => window.removeEventListener('hashchange', cb);
}

export function useRoute(): Route {
  const hash = useSyncExternalStore(subscribe, () => window.location.hash, () => '');
  return parseHash(hash);
}

/** Update query parameters of the current route without adding history entries. */
export function setParams(patch: Record<string, string | null>): void {
  const r = parseHash(window.location.hash);
  const p: Record<string, string | null> = {};
  r.params.forEach((v, k) => (p[k] = v));
  Object.assign(p, patch);
  navigate(href(r.view, r.id, p), true);
}
