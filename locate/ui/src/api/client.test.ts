import { describe, expect, it } from 'vitest';
import { ApiClient, ApiError, AuthError, ConflictError, NetworkError, ValidationError, fieldProblems } from './client';
import type { SystemConfig } from './types.gen';

type Call = { url: string; init: RequestInit };

function fakeFetch(responses: Array<{ status: number; body?: unknown; headers?: Record<string, string> } | Error>) {
  const calls: Call[] = [];
  const impl = async (url: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    calls.push({ url: String(url), init: init ?? {} });
    const r = responses.shift();
    if (!r) throw new Error('no more responses');
    if (r instanceof Error) throw r;
    const text = r.body === undefined ? '' : typeof r.body === 'string' ? r.body : JSON.stringify(r.body);
    return new Response(text, { status: r.status, headers: r.headers });
  };
  return { impl: impl as typeof fetch, calls };
}

const cfg = { name: 'x', satellites: [], sites: [] } as unknown as SystemConfig;

describe('ApiClient', () => {
  it('sends the bearer token when one was supplied, and none otherwise', async () => {
    const f = fakeFetch([{ status: 200, body: { version: '1' } }, { status: 200, body: {} }]);
    let token: string | null = 'secret-token';
    const c = new ApiClient({ fetchImpl: f.impl, getToken: () => token });
    await c.meta();
    token = null;
    await c.status();
    expect(f.calls[0]!.url).toBe('/api/v1/meta');
    expect((f.calls[0]!.init.headers as Record<string, string>).Authorization).toBe('Bearer secret-token');
    expect((f.calls[1]!.init.headers as Record<string, string>).Authorization).toBeUndefined();
  });

  it('returns the ETag of GET /config and sends it back in If-Match', async () => {
    const env = { version: 7, config: cfg, warnings: [] };
    const f = fakeFetch([{ status: 200, body: env, headers: { ETag: '"7"' } }, { status: 200, body: { ...env, version: 8 }, headers: { ETag: '"8"' } }]);
    const c = new ApiClient({ fetchImpl: f.impl });
    const got = await c.getConfig();
    expect(got.etag).toBe('"7"');
    const saved = await c.putConfig(cfg, got.etag ?? '');
    expect((f.calls[1]!.init.headers as Record<string, string>)['If-Match']).toBe('"7"');
    expect(f.calls[1]!.init.method).toBe('PUT');
    expect(saved.etag).toBe('"8"');
    expect(saved.data.version).toBe(8);
  });

  it('derives an ETag from the version when the server sends none', async () => {
    const f = fakeFetch([{ status: 200, body: { version: 3, config: cfg, warnings: [] } }]);
    expect((await new ApiClient({ fetchImpl: f.impl }).getConfig()).etag).toBe('"3"');
  });

  it('turns 409 into a ConflictError carrying the current version', async () => {
    const f = fakeFetch([
      { status: 409, body: { detail: 'version 7 is not current (current 8)', current_version: 8 } },
      { status: 409, body: { detail: 'stale' }, headers: { ETag: '"12"' } },
    ]);
    const c = new ApiClient({ fetchImpl: f.impl });
    const e1 = await c.putConfig(cfg, '"7"').catch((e: unknown) => e);
    expect(e1).toBeInstanceOf(ConflictError);
    expect((e1 as ConflictError).currentVersion).toBe(8);
    expect((e1 as ConflictError).message).toContain('not current');
    const e2 = await c.putConfig(cfg, '"7"').catch((e: unknown) => e);
    expect((e2 as ConflictError).currentVersion).toBe(12);
  });

  it('turns 422 with a ValidateResult body into field problems', async () => {
    const body = { ok: false, errors: [{ loc: 'sites.0.lat_deg', msg: 'Input should be less than or equal to 90' }], warnings: [{ loc: 'sites.2.channels.0.antenna', msg: 'reflector' }] };
    const f = fakeFetch([{ status: 422, body }]);
    const e = await new ApiClient({ fetchImpl: f.impl }).putConfig(cfg, '"1"').catch((x: unknown) => x);
    expect(e).toBeInstanceOf(ValidationError);
    expect((e as ValidationError).errors).toEqual(body.errors);
    expect((e as ValidationError).warnings).toEqual(body.warnings);
  });

  it('reads the FieldErrorResponse body of a 422 (detail, errors, warnings)', async () => {
    const body = { detail: '1 field problem', errors: [{ loc: 'satellites.0.orbit.line1', msg: 'Value error, TLE checksum mismatch: expected 7, found 3' }], warnings: [] };
    const f = fakeFetch([{ status: 422, body }]);
    const e = await new ApiClient({ fetchImpl: f.impl }).putConfig(cfg, '"1"').catch((x: unknown) => x);
    expect(e).toBeInstanceOf(ValidationError);
    expect((e as ValidationError).message).toBe('1 field problem');
    expect((e as ValidationError).errors[0]!.loc).toBe('satellites.0.orbit.line1');
    // pydantic's "Value error, " prefix is not shown to the operator
    expect((e as ValidationError).errors[0]!.msg).toBe('TLE checksum mismatch: expected 7, found 3');
  });

  it('turns FastAPI 422 detail lists into dotted locations without the body prefix', async () => {
    const body = { detail: [{ loc: ['body', 'pairs', 0], msg: 'no grid', type: 'value_error' }] };
    expect(fieldProblems(body).errors).toEqual([{ loc: 'pairs.0', msg: 'no grid' }]);
    const f = fakeFetch([{ status: 422, body }]);
    const e = await new ApiClient({ fetchImpl: f.impl }).accuracy({ pairs: [['a', 'b']], emitter: { eirp_dbw: 1, bandwidth_hz: 1 }, f_e_hz: 1 }).catch((x: unknown) => x);
    expect((e as ValidationError).errors[0]!.loc).toBe('pairs.0');
  });

  it('raises AuthError on 401 and 403', async () => {
    const f = fakeFetch([{ status: 401, body: { detail: 'missing token' } }, { status: 403, body: {} }]);
    const c = new ApiClient({ fetchImpl: f.impl });
    await expect(c.meta()).rejects.toBeInstanceOf(AuthError);
    await expect(c.meta()).rejects.toBeInstanceOf(AuthError);
  });

  it('raises NetworkError when the server cannot be reached', async () => {
    const f = fakeFetch([new TypeError('Failed to fetch')]);
    const e = await new ApiClient({ fetchImpl: f.impl }).status().catch((x: unknown) => x);
    expect(e).toBeInstanceOf(NetworkError);
    expect((e as NetworkError).status).toBe(0);
    expect((e as NetworkError).message).toContain('Failed to fetch');
  });

  it('raises ApiError with the server text on other failures, including non-JSON bodies', async () => {
    const f = fakeFetch([{ status: 500, body: 'Internal Server Error' }, { status: 404, body: { detail: 'not simulating' } }]);
    const c = new ApiClient({ fetchImpl: f.impl });
    const e = await c.status().catch((x: unknown) => x);
    expect(e).toBeInstanceOf(ApiError);
    expect((e as ApiError).status).toBe(500);
    expect((e as ApiError).message).toBe('Internal Server Error');
    await expect(c.scenario()).rejects.toMatchObject({ status: 404, message: 'not simulating' });
  });

  it('encodes ids and query parameters', async () => {
    const f = fakeFetch([{ status: 200, body: [] }, { status: 200, body: {} }, { status: 200, body: [] }]);
    const c = new ApiClient({ fetchImpl: f.impl });
    await c.events({ state: 'active', limit: 50 });
    await c.event('EV/1 2');
    await c.footprints(10, '2026-11-13T18:00:00Z');
    expect(f.calls[0]!.url).toBe('/api/v1/events?state=active&limit=50');
    expect(f.calls[1]!.url).toBe('/api/v1/events/EV%2F1%202');
    expect(f.calls[2]!.url).toBe('/api/v1/geometry/footprints?min_elevation_deg=10&t=2026-11-13T18%3A00%3A00Z');
  });
});
