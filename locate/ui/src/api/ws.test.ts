import { describe, expect, it, vi } from 'vitest';
import { backoffDelay, socketUrl, UiSocket, type LinkState, type WsMessage } from './ws';
import { encodePsdFrame, type PsdFrame } from './plb1';

class FakeWs {
  static last: FakeWs | null = null;
  binaryType = 'blob';
  readyState = 0;
  sent: string[] = [];
  onopen: ((ev: unknown) => void) | null = null;
  onclose: ((ev: { code: number; reason: string }) => void) | null = null;
  onerror: ((ev: unknown) => void) | null = null;
  onmessage: ((ev: { data: unknown }) => void) | null = null;
  constructor(readonly url: string) {
    FakeWs.last = this;
  }
  send(d: string) {
    this.sent.push(d);
  }
  close(code = 1000, reason = '') {
    this.readyState = 3;
    this.onclose?.({ code, reason });
  }
  open() {
    this.readyState = 1;
    this.onopen?.({});
  }
}

describe('socket', () => {
  it('backs off 1 s doubling to 30 s with up to 20 % jitter', () => {
    const none = () => 0;
    expect([1, 2, 3, 4, 5, 6, 7].map((n) => backoffDelay(n, 1000, 30000, 0.2, none))).toEqual([1000, 2000, 4000, 8000, 16000, 30000, 30000]);
    expect(backoffDelay(1, 1000, 30000, 0.2, () => 1)).toBe(1200);
  });

  it('builds the socket URL with the token query', () => {
    expect(socketUrl({ protocol: 'https:', host: 'ops.example:8443' }, 'a b')).toBe('wss://ops.example:8443/ws/ui?token=a%20b');
    expect(socketUrl({ protocol: 'http:', host: 'localhost:5173' }, null)).toBe('ws://localhost:5173/ws/ui');
  });

  it('dispatches text and binary messages, re-subscribes after a reconnect and ignores unknown types', () => {
    vi.useFakeTimers();
    const messages: WsMessage[] = [];
    const frames: PsdFrame[] = [];
    const states: LinkState[] = [];
    const s = new UiSocket({
      url: () => 'ws://x/ws/ui',
      create: (u) => new FakeWs(u),
      random: () => 0,
      handlers: { onMessage: (m) => messages.push(m), onFrame: (f) => frames.push(f), onState: (st) => states.push(st) },
    });
    s.subscribe([['ch-b', 'm1'], ['ch-a', 'm2']]);
    s.start();
    const ws1 = FakeWs.last!;
    ws1.open();
    expect(JSON.parse(ws1.sent[0]!)).toEqual({ type: 'subscribe', psd: [['ch-a', 'm2'], ['ch-b', 'm1']] });
    ws1.onmessage?.({ data: JSON.stringify({ type: 'config', config_version: 9 }) });
    ws1.onmessage?.({ data: JSON.stringify({ type: 'future_thing' }) });
    const buf = encodePsdFrame({ channel_id: 'c', monitor_id: 'm', t_ns: '1', center_hz: 1, span_hz: 1, nfft: 2, seq: 0, has_baseline: false, detections: [] }, new Float32Array(2), null);
    ws1.onmessage?.({ data: buf });
    expect(messages).toEqual([{ type: 'config', config_version: 9 }]);
    expect(frames).toHaveLength(1);
    expect(states.at(-1)?.kind).toBe('live');
    ws1.close(1006, '');
    expect(states.at(-1)).toMatchObject({ kind: 'reconnecting' });
    vi.advanceTimersByTime(1000);
    const ws2 = FakeWs.last!;
    expect(ws2).not.toBe(ws1);
    ws2.open();
    expect(ws2.sent).toHaveLength(1);
    s.stop();
    expect(states.at(-1)).toEqual({ kind: 'stopped' });
    vi.useRealTimers();
  });

  it('reconnects when the server goes silent', () => {
    vi.useFakeTimers();
    const states: LinkState[] = [];
    const s = new UiSocket({ url: () => 'ws://x', create: (u) => new FakeWs(u), random: () => 0, silenceMs: 5000, handlers: { onMessage: () => {}, onFrame: () => {}, onState: (st) => states.push(st) } });
    s.start();
    FakeWs.last!.open();
    vi.advanceTimersByTime(5001);
    expect(states.at(-1)).toMatchObject({ kind: 'reconnecting', reason: 'nothing received for 5 s' });
    s.stop();
    vi.useRealTimers();
  });

  it('schedules one reconnect for a silent socket even when its close event comes late', () => {
    vi.useFakeTimers();
    const states: LinkState[] = [];
    const created: FakeWs[] = [];
    // a dead link: close() does not fire onclose until much later
    class SlowCloseWs extends FakeWs {
      pendingClose: (() => void) | null = null;
      override close(code = 1000, reason = '') {
        const cb = this.onclose;
        this.pendingClose = () => cb?.({ code, reason });
      }
    }
    const s = new UiSocket({
      url: () => 'ws://x',
      create: (u) => {
        const w = new SlowCloseWs(u);
        created.push(w);
        return w;
      },
      random: () => 0,
      silenceMs: 5000,
      handlers: { onMessage: () => {}, onFrame: () => {}, onState: (st) => states.push(st) },
    });
    s.start();
    created[0]!.open();
    vi.advanceTimersByTime(5001);
    const reconnecting = states.filter((st) => st.kind === 'reconnecting').length;
    (created[0] as SlowCloseWs).pendingClose?.(); // the old socket's close finally arrives
    expect(states.filter((st) => st.kind === 'reconnecting').length).toBe(reconnecting);
    vi.advanceTimersByTime(1000);
    expect(created).toHaveLength(2);
    s.stop();
    vi.useRealTimers();
  });
});
