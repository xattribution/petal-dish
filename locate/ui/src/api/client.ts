// Typed REST client for PROTOCOL.md section B (/api/v1). Errors are classes the screens
// can act on: AuthError (ask for a token), ConflictError (409: someone saved first),
// ValidationError (422: field problems to show on the form), NetworkError (server not
// reachable), ApiError (anything else, with the server's own words).
import type {
  AccuracyGrid,
  AccuracyRequest,
  ConfigEnvelope,
  Event,
  EventSummary,
  FieldProblem,
  Footprint,
  LinkBudgetRequest,
  LinkBudgetResponse,
  LopCollection,
  LookAngle,
  Meta,
  Preset,
  Scenario,
  ScenarioEnvelope,
  SimTruth,
  Status,
  Subpoint,
  SystemConfig,
  ValidateResult,
} from './types.gen';

export const API_PREFIX = '/api/v1';

export class ApiError extends Error {
  override name = 'ApiError';
  constructor(
    message: string,
    readonly status: number,
    readonly body: unknown = null,
  ) {
    super(message);
  }
}

export class NetworkError extends ApiError {
  override name = 'NetworkError';
  constructor(message: string) {
    super(message, 0);
  }
}

export class AuthError extends ApiError {
  override name = 'AuthError';
}

export class ConflictError extends ApiError {
  override name = 'ConflictError';
  constructor(
    message: string,
    body: unknown,
    /** the server's current version when it said so (body.current_version or the ETag) */
    readonly currentVersion: number | null,
  ) {
    super(message, 409, body);
  }
}

export class ValidationError extends ApiError {
  override name = 'ValidationError';
  constructor(
    message: string,
    body: unknown,
    readonly errors: FieldProblem[],
    readonly warnings: FieldProblem[],
  ) {
    super(message, 422, body);
  }
}

export interface ClientOptions {
  baseUrl?: string;
  getToken?: () => string | null;
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
}

export interface Tagged<T> {
  data: T;
  etag: string | null;
}

type Json = null | boolean | number | string | Json[] | { [k: string]: Json };

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

/** Field problems from a ValidateResult body or from FastAPI's {detail: [{loc, msg}]}. */
export function fieldProblems(body: unknown): { errors: FieldProblem[]; warnings: FieldProblem[] } {
  if (isRecord(body) && Array.isArray(body.errors)) {
    const pick = (list: unknown): FieldProblem[] =>
      Array.isArray(list)
        ? list.filter(isRecord).map((p) => ({ loc: String(p.loc ?? ''), msg: String(p.msg ?? '') }))
        : [];
    return { errors: pick(body.errors), warnings: pick(body.warnings) };
  }
  if (isRecord(body) && Array.isArray(body.detail)) {
    const errors = body.detail.filter(isRecord).map((d) => {
      const loc = Array.isArray(d.loc) ? d.loc.map(String) : [String(d.loc ?? '')];
      // FastAPI prefixes request-body locations with "body"; the configuration editor
      // addresses fields from the document root.
      const path = loc[0] === 'body' ? loc.slice(1) : loc;
      return { loc: path.join('.'), msg: String(d.msg ?? '') };
    });
    return { errors, warnings: [] };
  }
  if (isRecord(body) && typeof body.detail === 'string') return { errors: [{ loc: '', msg: body.detail }], warnings: [] };
  return { errors: [], warnings: [] };
}

function messageOf(body: unknown, fallback: string): string {
  if (isRecord(body)) {
    if (typeof body.detail === 'string') return body.detail;
    if (typeof body.message === 'string') return body.message;
  }
  if (typeof body === 'string' && body.trim()) return body.trim().slice(0, 300);
  return fallback;
}

function versionFromEtag(etag: string | null): number | null {
  if (!etag) return null;
  const m = etag.match(/(\d+)/);
  return m ? Number(m[1]) : null;
}

export class ApiClient {
  private readonly base: string;
  private readonly getToken: () => string | null;
  private readonly fetchImpl: typeof fetch;
  private readonly timeoutMs: number;

  constructor(opts: ClientOptions = {}) {
    this.base = (opts.baseUrl ?? '') + API_PREFIX;
    this.getToken = opts.getToken ?? (() => null);
    this.fetchImpl = opts.fetchImpl ?? ((input, init) => fetch(input, init));
    this.timeoutMs = opts.timeoutMs ?? 30000;
  }

  async request<T>(method: string, path: string, init: { body?: unknown; headers?: Record<string, string>; timeoutMs?: number } = {}): Promise<Tagged<T>> {
    const headers: Record<string, string> = { Accept: 'application/json', ...init.headers };
    const token = this.getToken();
    if (token) headers.Authorization = `Bearer ${token}`;
    let body: string | undefined;
    if (init.body !== undefined) {
      headers['Content-Type'] = 'application/json';
      body = JSON.stringify(init.body);
    }
    const ctrl = new AbortController();
    const timeoutMs = init.timeoutMs ?? this.timeoutMs;
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    let res: Response;
    try {
      res = await this.fetchImpl(this.base + path, { method, headers, body, signal: ctrl.signal });
    } catch (e) {
      const why = (e as Error).name === 'AbortError' ? `no answer within ${Math.round(timeoutMs / 1000)} s` : (e as Error).message;
      throw new NetworkError(`Could not reach the server (${method} ${path}): ${why}`);
    } finally {
      clearTimeout(timer);
    }
    const etag = res.headers.get('ETag');
    const text = await res.text();
    let parsed: unknown = null;
    if (text) {
      try {
        parsed = JSON.parse(text) as Json;
      } catch {
        parsed = text;
      }
    }
    if (res.ok) return { data: parsed as T, etag };
    const what = `${method} ${path} answered ${res.status}`;
    switch (res.status) {
      case 401:
      case 403:
        throw new AuthError(messageOf(parsed, `${what}: an API token is required`), res.status, parsed);
      case 409: {
        const cur = isRecord(parsed) && typeof parsed.current_version === 'number' ? parsed.current_version : versionFromEtag(etag);
        throw new ConflictError(messageOf(parsed, `${what}: the configuration changed on the server`), parsed, cur);
      }
      case 422: {
        const { errors, warnings } = fieldProblems(parsed);
        throw new ValidationError(messageOf(parsed, `${what}: ${errors.length} field problem${errors.length === 1 ? '' : 's'}`), parsed, errors, warnings);
      }
      default:
        throw new ApiError(messageOf(parsed, what), res.status, parsed);
    }
  }

  meta(): Promise<Meta> {
    return this.request<Meta>('GET', '/meta').then((r) => r.data);
  }

  async getConfig(): Promise<Tagged<ConfigEnvelope>> {
    const r = await this.request<ConfigEnvelope>('GET', '/config');
    return { data: r.data, etag: r.etag ?? `"${r.data.version}"` };
  }

  /** Save with If-Match; ConflictError on 409, ValidationError on 422. */
  async putConfig(config: SystemConfig, etag: string): Promise<Tagged<ConfigEnvelope>> {
    const r = await this.request<ConfigEnvelope>('PUT', '/config', { body: config, headers: { 'If-Match': etag } });
    return { data: r.data, etag: r.etag ?? `"${r.data.version}"` };
  }

  validateConfig(config: SystemConfig): Promise<ValidateResult> {
    return this.request<ValidateResult>('POST', '/config/validate', { body: config }).then((r) => r.data);
  }

  configSchema(): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>('GET', '/config/schema').then((r) => r.data);
  }

  status(): Promise<Status> {
    return this.request<Status>('GET', '/status').then((r) => r.data);
  }

  events(params: { state?: string; limit?: number } = {}): Promise<EventSummary[]> {
    const q = new URLSearchParams();
    if (params.state) q.set('state', params.state);
    if (params.limit) q.set('limit', String(params.limit));
    const s = q.toString();
    return this.request<EventSummary[]>('GET', `/events${s ? `?${s}` : ''}`).then((r) => r.data);
  }

  event(id: string): Promise<Event> {
    return this.request<Event>('GET', `/events/${encodeURIComponent(id)}`).then((r) => r.data);
  }

  closeEvent(id: string): Promise<unknown> {
    return this.request<unknown>('POST', `/events/${encodeURIComponent(id)}/close`).then((r) => r.data);
  }

  lop(id: string): Promise<LopCollection> {
    return this.request<LopCollection>('GET', `/events/${encodeURIComponent(id)}/lop`).then((r) => r.data);
  }

  subpoints(t?: string): Promise<Subpoint[]> {
    return this.request<Subpoint[]>('GET', `/geometry/subpoints${t ? `?t=${encodeURIComponent(t)}` : ''}`).then((r) => r.data);
  }

  footprints(minElevationDeg: number, t?: string): Promise<Footprint[]> {
    const q = new URLSearchParams({ min_elevation_deg: String(minElevationDeg) });
    if (t) q.set('t', t);
    return this.request<Footprint[]>('GET', `/geometry/footprints?${q}`).then((r) => r.data);
  }

  lookAngles(t?: string): Promise<LookAngle[]> {
    return this.request<LookAngle[]>('GET', `/geometry/look-angles${t ? `?t=${encodeURIComponent(t)}` : ''}`).then((r) => r.data);
  }

  /** The server gives up after 120 s (504); wait a little longer than that. */
  accuracy(req: AccuracyRequest): Promise<AccuracyGrid> {
    return this.request<AccuracyGrid>('POST', '/analysis/accuracy', { body: req, timeoutMs: 130000 }).then((r) => r.data);
  }

  linkBudget(req: LinkBudgetRequest): Promise<LinkBudgetResponse> {
    return this.request<LinkBudgetResponse>('POST', '/analysis/link-budget', { body: req }).then((r) => r.data);
  }

  scenario(): Promise<ScenarioEnvelope> {
    return this.request<ScenarioEnvelope>('GET', '/sim/scenario').then((r) => r.data);
  }

  putScenario(s: Scenario): Promise<ScenarioEnvelope> {
    return this.request<ScenarioEnvelope>('PUT', '/sim/scenario', { body: s }).then((r) => r.data);
  }

  presets(): Promise<Preset[]> {
    return this.request<Preset[]>('GET', '/sim/presets').then((r) => r.data);
  }

  loadPreset(name: string): Promise<ScenarioEnvelope> {
    return this.request<ScenarioEnvelope>('POST', `/sim/presets/${encodeURIComponent(name)}/load`).then((r) => r.data);
  }

  resetSim(): Promise<ScenarioEnvelope> {
    return this.request<ScenarioEnvelope>('POST', '/sim/reset').then((r) => r.data);
  }

  truth(): Promise<SimTruth> {
    return this.request<SimTruth>('GET', '/sim/truth').then((r) => r.data);
  }
}
