// Lazily loaded screens that can be loaded again after a failure. React.lazy keeps a
// rejected import for good, and the browser keeps a failed module URL as failed for the
// life of the page, so a screen whose chunk failed to load (server down, network cut)
// would stay broken until a full reload. Here a failed load is forgotten, the next
// render of the screen gets a fresh lazy component, and the chunk is imported again
// under a new URL (the same file with a query), which the browser fetches anew.
import { lazy, type ComponentType, type LazyExoticComponent } from 'react';

/** The chunk URL a failed dynamic import names (Chromium and Firefox put it in the message), or null. */
export function failedChunkUrl(error: unknown): string | null {
  const text = error instanceof Error ? error.message : String(error);
  const m = text.match(/(https?:\/\/[^\s'"]+?\.m?js)(?:\?[^\s'"]*)?(?=$|[\s'"])/);
  return m ? (m[1] ?? null) : null;
}

/**
 * A loader that shares one load at a time and forgets a failed one, so the next call
 * tries again; after a failure that named its chunk, the retry imports that chunk under
 * a new URL with retryImport.
 */
export function retryingLoader<T>(
  load: () => Promise<T>,
  retryImport?: (url: string) => Promise<T>,
): { get: () => Promise<T>; takeFailure: () => boolean } {
  let promise: Promise<T> | null = null;
  let failed = false;
  let failedUrl: string | null = null;
  let attempt = 0;
  return {
    get() {
      if (!promise) {
        const url = failedUrl;
        attempt += 1;
        const p = url && retryImport ? retryImport(`${url}?retry=${attempt}`) : load();
        promise = p.catch((e: unknown) => {
          promise = null;
          failed = true;
          failedUrl = failedChunkUrl(e) ?? failedUrl;
          throw e;
        });
      }
      return promise;
    },
    /** true once after a load failed (and clears the mark) */
    takeFailure() {
      const f = failed;
      failed = false;
      return f;
    },
  };
}

export interface RetryableView<P> {
  /** The lazy component to render; a new one after a failed load, so it is imported again. */
  current(): LazyExoticComponent<ComponentType<P>>;
  /** Load the chunk ahead of time; a failure is forgotten and the screen retries when shown. */
  prefetch(): void;
}

/**
 * load: the bundler's import of the screen's module (import('./views/Network')); pick:
 * the component from that module. A retry after a failed chunk imports the same file
 * under a new URL and picks from it the same way.
 */
export function retryableLazy<M, P extends object>(load: () => Promise<M>, pick: (m: M) => ComponentType<P>): RetryableView<P> {
  const loader = retryingLoader(
    () => load().then(pick),
    (url) => (import(/* @vite-ignore */ url) as Promise<M>).then(pick),
  );
  const make = () => lazy(() => loader.get().then((c) => ({ default: c })));
  let comp = make();
  return {
    current() {
      if (loader.takeFailure()) comp = make();
      return comp;
    },
    prefetch() {
      void loader.get().catch(() => undefined);
    },
  };
}
