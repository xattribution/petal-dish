// Theme setting (Auto follows the OS, or Dark / Light by choice) and the token values that
// canvas drawing needs (canvas cannot use var()).
import { useSyncExternalStore } from 'react';

export type ThemeSetting = 'auto' | 'dark' | 'light';
const KEY = 'petal.theme';

function readSetting(): ThemeSetting {
  try {
    const v = localStorage.getItem(KEY);
    if (v === 'dark' || v === 'light' || v === 'auto') return v;
  } catch {
    /* storage blocked */
  }
  return 'auto';
}

let setting: ThemeSetting = readSetting();
const ls = new Set<() => void>();
const media = typeof window !== 'undefined' ? window.matchMedia('(prefers-color-scheme: light)') : null;

function resolved(): 'dark' | 'light' {
  if (setting === 'auto') return media?.matches ? 'light' : 'dark';
  return setting;
}

let cache: Record<string, string> = {};
function apply(): void {
  document.documentElement.dataset.theme = resolved();
  cache = {};
  for (const l of ls) l();
}

export function initTheme(): void {
  apply();
  media?.addEventListener('change', () => setting === 'auto' && apply());
}

export function setTheme(s: ThemeSetting): void {
  setting = s;
  try {
    localStorage.setItem(KEY, s);
  } catch {
    /* storage blocked: the choice lasts for this page */
  }
  apply();
}

export function useTheme(): { setting: ThemeSetting; resolved: 'dark' | 'light' } {
  const r = useSyncExternalStore(
    (l) => {
      ls.add(l);
      return () => ls.delete(l);
    },
    () => `${setting}|${resolved()}`,
    () => 'auto|dark',
  );
  const [s, res] = r.split('|') as [ThemeSetting, 'dark' | 'light'];
  return { setting: s, resolved: res };
}

export function onThemeChange(l: () => void): () => void {
  ls.add(l);
  return () => ls.delete(l);
}

/** The value of a color token, e.g. color('--fix'). */
export function color(token: string): string {
  let v = cache[token];
  if (v === undefined) {
    v = getComputedStyle(document.documentElement).getPropertyValue(token).trim() || '#888';
    cache[token] = v;
  }
  return v;
}
