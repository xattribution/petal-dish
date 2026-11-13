import { describe, expect, it } from 'vitest';
import golden from './testdata/console_psd.json';
import { decodePsdFrame, encodePsdFrame, FrameError, splitFrame } from './plb1';

function fromB64(b64: string): ArrayBuffer {
  const bin = Buffer.from(b64, 'base64');
  return bin.buffer.slice(bin.byteOffset, bin.byteOffset + bin.byteLength) as ArrayBuffer;
}

describe('PLB1 console spectrum frames', () => {
  it('decodes a frame built by the Python encoder, with baseline and detections', () => {
    const f = decodePsdFrame(fromB64(golden.with_baseline));
    expect(f.header.channel_id).toBe('naha-pac');
    expect(f.header.monitor_id).toBe('muos-pac:shared-1');
    expect(f.header.t_ns).toBe(golden.t_ns);
    expect(typeof f.header.t_ns).toBe('string');
    expect(f.header.nfft).toBe(8);
    expect(f.header.seq).toBe(5);
    expect(f.header.has_baseline).toBe(true);
    expect(Array.from(f.psd)).toEqual(golden.psd);
    expect(Array.from(f.baseline ?? [])).toEqual(golden.baseline.map((v) => Math.fround(v)));
    expect(f.header.detections).toEqual(golden.detections);
  });
  it('decodes a frame without baseline', () => {
    const f = decodePsdFrame(fromB64(golden.without_baseline));
    expect(f.baseline).toBeNull();
    expect(f.header.seq).toBe(6);
    expect(f.header.detections).toEqual([]);
  });
  it('accepts unpadded headers (payload not 4-byte aligned)', () => {
    const psd = new Float32Array([1, 2, 3, 4]);
    let header = { channel_id: 'c', monitor_id: 'm', t_ns: '1', center_hz: 1e6, span_hz: 1e3, nfft: 4, seq: 1, has_baseline: false, detections: [] };
    let buf = encodePsdFrame(header, psd, null, false);
    while ((8 + new DataView(buf).getUint32(4, true)) % 4 === 0) {
      header = { ...header, channel_id: `${header.channel_id}c` };
      buf = encodePsdFrame(header, psd, null, false);
    }
    const view = new DataView(buf);
    expect((8 + view.getUint32(4, true)) % 4).not.toBe(0);
    expect(Array.from(decodePsdFrame(buf).psd)).toEqual([1, 2, 3, 4]);
    expect(Array.from(decodePsdFrame(encodePsdFrame(header, psd, psd)).baseline ?? [])).toEqual([1, 2, 3, 4]);
  });
  it('rejects a wrong magic, an oversized or truncated header and a short payload', () => {
    const good = new Uint8Array(fromB64(golden.with_baseline));
    const bad = good.slice();
    bad[0] = 0x58;
    expect(() => decodePsdFrame(bad.buffer)).toThrow(FrameError);
    const big = good.slice();
    new DataView(big.buffer).setUint32(4, 70000, true);
    expect(() => splitFrame(big.buffer)).toThrow(/exceeds/);
    expect(() => splitFrame(good.slice(0, 20).buffer)).toThrow(/truncated/);
    expect(() => decodePsdFrame(good.slice(0, good.length - 4).buffer)).toThrow(/payload/);
    expect(() => decodePsdFrame(new ArrayBuffer(4))).toThrow(/prefix/);
  });
  it('requires t_ns as a decimal string', () => {
    const header = { channel_id: 'c', monitor_id: 'm', t_ns: '1', center_hz: 1, span_hz: 1, nfft: 2, seq: 0, has_baseline: false, detections: [] };
    const buf = encodePsdFrame(header, new Float32Array(2), null);
    const text = new TextDecoder().decode(new Uint8Array(buf, 8, new DataView(buf).getUint32(4, true)));
    const forged = encodePsdFrame({ ...header, t_ns: 1763056800123456789 as unknown as string }, new Float32Array(2), null);
    expect(text).toContain('"t_ns":"1"');
    expect(() => decodePsdFrame(forged)).toThrow(/t_ns/);
  });
});
