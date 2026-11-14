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

/** Go to a route (`href`): a new history entry, or with `replace` the current one.
 *  The hashchange is dispatched synchronously in both cases, so the view (and the
 *  focus a number key moves into it) changes before the next key is handled; setting
 *  `location.hash` instead would fire it only in a later task. Going to the route
 *  already shown adds no entry. */
export function navigate(to: string, replace = false): void {
  const target = to.startsWith('#') ? to : `#${to}`;
  if (!replace && window.location.hash === target) return;
  const oldURL = window.location.href;
  if (replace) history.replaceState(null, '', target);
  else history.pushState(null, '', target);
  window.dispatchEvent(new HashChangeEvent('hashchange', { oldURL, newURL: window.location.href }));
}

function subscribe(cb: () => void): () => void {
  // Back and Forward between entries this module pushed fire popstate (and hashchange)
  window.addEventListener('hashchange', cb);
  window.addEventListener('popstate', cb);
  return () => {
    window.removeEventListener('hashchange', cb);
    window.removeEventListener('popstate', cb);
  };
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
