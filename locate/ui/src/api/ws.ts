// One WebSocket to /ws/ui (PROTOCOL.md section B): status about once a second, event,
// config and sim messages as text; spectrum frames as PLB1 binary. Reconnects with
// exponential backoff (1 s doubling to 30 s, plus up to 20 % jitter) and re-sends the
// current spectrum subscription after every reconnect.
import { decodePsdFrame, FrameError, type PsdFrame } from './plb1';
import type { WsConfigMessage, WsEventMessage, WsSimMessage, WsStatusMessage } from './types.gen';

export type WsMessage = WsStatusMessage | WsEventMessage | WsConfigMessage | WsSimMessage;

export type LinkState =
  | { kind: 'connecting'; attempt: number }
  | { kind: 'live'; since: number }
  | { kind: 'reconnecting'; attempt: number; retryAt: number; reason: string; authRefused?: boolean }
  | { kind: 'stopped' };

export interface SocketHandlers {
  onMessage: (msg: WsMessage) => void;
  onFrame: (frame: PsdFrame) => void;
  onState: (state: LinkState) => void;
  onProtocolError?: (error: string) => void;
}

interface WsLike {
  binaryType: string;
  readyState: number;
  onopen: ((ev: unknown) => void) | null;
  onclose: ((ev: { code: number; reason: string }) => void) | null;
  onerror: ((ev: unknown) => void) | null;
  onmessage: ((ev: { data: unknown }) => void) | null;
  send(data: string): void;
  close(code?: number, reason?: string): void;
}

export interface SocketOptions {
  url: () => string;
  handlers: SocketHandlers;
  create?: (url: string) => WsLike;
  minDelayMs?: number;
  maxDelayMs?: number;
  jitter?: number;
  random?: () => number;
  now?: () => number;
  /** reconnect when nothing arrives for this long while open (status comes every second) */
  silenceMs?: number;
}

/** Backoff delay before attempt n (n >= 1): min * 2^(n-1), capped, plus up to `jitter` of it. */
export function backoffDelay(attempt: number, minMs: number, maxMs: number, jitter: number, random: () => number): number {
  const base = Math.min(maxMs, minMs * 2 ** Math.max(0, attempt - 1));
  return Math.round(base * (1 + jitter * random()));
}

/** Absolute ws(s):// URL of the console socket for the page's origin, with ?token= when given. */
export function socketUrl(location: { protocol: string; host: string }, token: string | null): string {
  const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const q = token ? `?token=${encodeURIComponent(token)}` : '';
  return `${scheme}//${location.host}/ws/ui${q}`;
}

const KNOWN = new Set(['status', 'event', 'config', 'sim']);

/** Close codes in the words an operator needs (PROTOCOL.md section A close codes). */
const CLOSE_WORD: Record<number, string> = {
  1001: 'server shutting down',
  1006: 'server not reachable',
  1011: 'server error',
  1013: 'server busy',
};

export class UiSocket {
  private ws: WsLike | null = null;
  private attempt = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private silenceTimer: ReturnType<typeof setTimeout> | null = null;
  private stopped = true;
  private subscription: Array<[string, string]> = [];
  private sentKey = '';
  private readonly o: Required<Omit<SocketOptions, 'create'>> & { create: (url: string) => WsLike };
  state: LinkState = { kind: 'stopped' };

  constructor(opts: SocketOptions) {
    this.o = {
      minDelayMs: 1000,
      maxDelayMs: 30000,
      jitter: 0.2,
      random: Math.random,
      now: Date.now,
      silenceMs: 10000,
      create: (url) => new WebSocket(url) as unknown as WsLike,
      ...opts,
    };
  }

  start(): void {
    if (!this.stopped) return;
    this.stopped = false;
    this.connect();
  }

  stop(): void {
    this.stopped = true;
    this.clearTimers();
    const ws = this.ws;
    this.ws = null;
    if (ws) {
      ws.onclose = null;
      ws.close(1000, 'console closed');
    }
    this.setState({ kind: 'stopped' });
  }

  /** Replace the set of spectrum windows streamed to this console. */
  subscribe(pairs: Array<[string, string]>): void {
    this.subscription = [...pairs].sort((a, b) => (a[0] + a[1]).localeCompare(b[0] + b[1]));
    this.flushSubscription();
  }

  /** Reconnect now (after a token change, for example). */
  restart(): void {
    this.stop();
    this.attempt = 0;
    this.start();
  }

  private setState(s: LinkState): void {
    this.state = s;
    this.o.handlers.onState(s);
  }

  private clearTimers(): void {
    if (this.timer) clearTimeout(this.timer);
    if (this.silenceTimer) clearTimeout(this.silenceTimer);
    this.timer = null;
    this.silenceTimer = null;
  }

  private armSilence(): void {
    if (this.silenceTimer) clearTimeout(this.silenceTimer);
    this.silenceTimer = setTimeout(() => {
      // drop the silent socket without waiting for its close handshake (a dead link may
      // never complete it), so exactly one reconnect is scheduled
      const ws = this.ws;
      this.ws = null;
      if (ws) {
        ws.onclose = null;
        ws.onmessage = null;
        ws.close(4000, 'silent');
      }
      this.scheduleReconnect(`nothing received for ${Math.round(this.o.silenceMs / 1000)} s`);
    }, this.o.silenceMs);
  }

  private flushSubscription(): void {
    const key = JSON.stringify(this.subscription);
    if (!this.ws || this.ws.readyState !== 1 || key === this.sentKey) return;
    this.ws.send(JSON.stringify({ type: 'subscribe', psd: this.subscription }));
    this.sentKey = key;
  }

  private connect(): void {
    this.clearTimers();
    this.attempt += 1;
    this.setState({ kind: 'connecting', attempt: this.attempt });
    let ws: WsLike;
    try {
      ws = this.o.create(this.o.url());
    } catch (e) {
      this.scheduleReconnect((e as Error).message);
      return;
    }
    this.ws = ws;
    this.sentKey = '';
    ws.binaryType = 'arraybuffer';
    ws.onopen = () => {
      this.armSilence();
      this.flushSubscription();
    };
    ws.onmessage = (ev) => {
      if (this.state.kind !== 'live') {
        this.attempt = 0;
        this.setState({ kind: 'live', since: this.o.now() });
      }
      this.armSilence();
      this.dispatch(ev.data);
    };
    ws.onerror = () => {
      /* the close event that follows carries the reason */
    };
    ws.onclose = (ev) => {
      if (this.ws !== ws) return;
      this.ws = null;
      const why = ev.code === 1008 ? 'the server refused the token' : (CLOSE_WORD[ev.code] ?? (ev.reason || `connection closed (code ${ev.code})`));
      this.scheduleReconnect(why, ev.code === 1008);
    };
  }

  private scheduleReconnect(reason: string, authRefused = false): void {
    if (this.stopped) return;
    this.clearTimers();
    const delay = backoffDelay(this.attempt, this.o.minDelayMs, this.o.maxDelayMs, this.o.jitter, this.o.random);
    this.setState({ kind: 'reconnecting', attempt: this.attempt, retryAt: this.o.now() + delay, reason, ...(authRefused ? { authRefused } : {}) });
    this.timer = setTimeout(() => this.connect(), delay);
  }

  private dispatch(data: unknown): void {
    if (data instanceof ArrayBuffer) {
      try {
        this.o.handlers.onFrame(decodePsdFrame(data));
      } catch (e) {
        this.o.handlers.onProtocolError?.(e instanceof FrameError ? e.message : String(e));
      }
      return;
    }
    if (typeof data !== 'string') return;
    let msg: unknown;
    try {
      msg = JSON.parse(data);
    } catch {
      this.o.handlers.onProtocolError?.('a text frame was not JSON');
      return;
    }
    if (typeof msg === 'object' && msg !== null && 'type' in msg && KNOWN.has(String((msg as { type: unknown }).type))) {
      this.o.handlers.onMessage(msg as WsMessage);
    }
    // unknown types are ignored (forward compatibility)
  }
}
