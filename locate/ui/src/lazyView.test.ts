// UX-2 / M2: a screen whose chunk failed to load is imported again, not kept broken.
import { describe, expect, it } from 'vitest';
import { failedChunkUrl, retryingLoader } from './lazyView';

describe('retrying loader', () => {
  it('imports again after a failure, and keeps a success', async () => {
    let calls = 0;
    const loader = retryingLoader(async () => {
      calls += 1;
      if (calls === 1) throw new Error('Failed to fetch dynamically imported module');
      return 'view';
    });
    await expect(loader.get()).rejects.toThrow('Failed to fetch');
    expect(loader.takeFailure()).toBe(true);
    expect(loader.takeFailure()).toBe(false);
    await expect(loader.get()).resolves.toBe('view');
    await expect(loader.get()).resolves.toBe('view');
    expect(calls).toBe(2);
    expect(loader.takeFailure()).toBe(false);
  });
  it('shares one load between callers', async () => {
    let calls = 0;
    const loader = retryingLoader(async () => {
      calls += 1;
      return 1;
    });
    await Promise.all([loader.get(), loader.get(), loader.get()]);
    expect(calls).toBe(1);
  });
});

describe('retry of a failed chunk under a new URL', () => {
  it('reads the chunk URL from the browser message', () => {
    expect(failedChunkUrl(new TypeError('Failed to fetch dynamically imported module: http://127.0.0.1:4391/assets/Network-D6k1PTXq.js'))).toBe('http://127.0.0.1:4391/assets/Network-D6k1PTXq.js');
    expect(failedChunkUrl(new TypeError('error loading dynamically imported module: https://h/assets/Plan-x.js?retry=2'))).toBe('https://h/assets/Plan-x.js');
    expect(failedChunkUrl(new TypeError('Importing a module script failed.'))).toBeNull();
  });
  it('imports the failed chunk again with a query, since the browser keeps the failed URL', async () => {
    const urls: string[] = [];
    let n = 0;
    const loader = retryingLoader(
      async () => {
        n += 1;
        throw new TypeError('Failed to fetch dynamically imported module: http://h/assets/Network-1.js');
      },
      async (url) => {
        urls.push(url);
        return 'view';
      },
    );
    await expect(loader.get()).rejects.toThrow();
    await expect(loader.get()).resolves.toBe('view');
    expect(n).toBe(1);
    expect(urls).toEqual(['http://h/assets/Network-1.js?retry=2']);
  });
});
