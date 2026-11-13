// PLB1 binary frames (PROTOCOL.md section A framing, section B console spectrum header):
//   bytes 0..3 "PLB1", bytes 4..7 header length H (uint32 LE), H bytes UTF-8 JSON header,
//   payload little-endian: float32[nfft] PSD, then float32[nfft] baseline when has_baseline.
import { isNsString } from '../time/ns';

export const MAGIC = 'PLB1';
export const MAX_HEADER_BYTES = 64 * 1024;

export interface PsdDetection {
  track_id: number;
  f_lo_hz: number;
  f_hi_hz: number;
  authorized_carrier_id: string | null;
  anomaly: boolean;
}

export interface PsdHeader {
  type: 'psd';
  channel_id: string;
  monitor_id: string;
  /** decimal-string nanoseconds; never converted to Number */
  t_ns: string;
  center_hz: number;
  span_hz: number;
  nfft: number;
  seq: number;
  has_baseline: boolean;
  detections: PsdDetection[];
}

export interface PsdFrame {
  header: PsdHeader;
  psd: Float32Array;
  baseline: Float32Array | null;
}

export class FrameError extends Error {
  override name = 'FrameError';
}

const decoder = new TextDecoder('utf-8', { fatal: true });
const littleEndianHost = new Uint8Array(new Uint32Array([1]).buffer)[0] === 1;

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

/** Split a PLB1 frame into its header object and payload bytes (no copy of the payload). */
export function splitFrame(buf: ArrayBuffer): { header: Record<string, unknown>; payload: DataView } {
  if (buf.byteLength < 8) throw new FrameError(`frame of ${buf.byteLength} bytes is shorter than the 8-byte prefix`);
  const bytes = new Uint8Array(buf);
  const magic = String.fromCharCode(bytes[0] ?? 0, bytes[1] ?? 0, bytes[2] ?? 0, bytes[3] ?? 0);
  if (magic !== MAGIC) throw new FrameError(`bad magic ${JSON.stringify(magic)}, expected PLB1`);
  const view = new DataView(buf);
  const h = view.getUint32(4, true);
  if (h > MAX_HEADER_BYTES) throw new FrameError(`header of ${h} bytes exceeds ${MAX_HEADER_BYTES}`);
  if (8 + h > buf.byteLength) throw new FrameError(`truncated header: ${h} bytes declared, ${buf.byteLength - 8} present`);
  let header: unknown;
  try {
    header = JSON.parse(decoder.decode(bytes.subarray(8, 8 + h)));
  } catch (e) {
    throw new FrameError(`header is not UTF-8 JSON: ${(e as Error).message}`);
  }
  if (!isRecord(header) || typeof header.type !== 'string' || !header.type) {
    throw new FrameError('header is not a JSON object with a string "type"');
  }
  return { header, payload: new DataView(buf, 8 + h) };
}

function readFloats(payload: DataView, offsetBytes: number, n: number): Float32Array {
  const start = payload.byteOffset + offsetBytes;
  // A typed array view needs a 4-byte aligned offset and a little-endian host; senders
  // pad the header so the payload is aligned, but receivers accept unpadded frames.
  if (littleEndianHost && start % 4 === 0) return new Float32Array(payload.buffer, start, n);
  const out = new Float32Array(n);
  for (let i = 0; i < n; i++) out[i] = payload.getFloat32(offsetBytes + 4 * i, true);
  return out;
}

function str(h: Record<string, unknown>, k: string): string {
  const v = h[k];
  if (typeof v !== 'string' || !v) throw new FrameError(`psd header: "${k}" must be a non-empty string`);
  return v;
}

function num(h: Record<string, unknown>, k: string): number {
  const v = h[k];
  if (typeof v !== 'number' || !Number.isFinite(v)) throw new FrameError(`psd header: "${k}" must be a finite number`);
  return v;
}

function detections(v: unknown): PsdDetection[] {
  if (v === undefined) return [];
  if (!Array.isArray(v)) throw new FrameError('psd header: "detections" must be a list');
  return v.map((d, i) => {
    if (!isRecord(d)) throw new FrameError(`psd header: detections[${i}] is not an object`);
    const lo = d.f_lo_hz;
    const hi = d.f_hi_hz;
    if (typeof lo !== 'number' || typeof hi !== 'number' || typeof d.track_id !== 'number') {
      throw new FrameError(`psd header: detections[${i}] needs track_id, f_lo_hz, f_hi_hz`);
    }
    const auth = d.authorized_carrier_id;
    return {
      track_id: d.track_id,
      f_lo_hz: lo,
      f_hi_hz: hi,
      authorized_carrier_id: typeof auth === 'string' ? auth : null,
      anomaly: d.anomaly === true,
    };
  });
}

/** Decode a console spectrum frame. Throws FrameError on anything that breaks the protocol. */
export function decodePsdFrame(buf: ArrayBuffer): PsdFrame {
  const { header: h, payload } = splitFrame(buf);
  if (h.type !== 'psd') throw new FrameError(`unexpected frame type ${JSON.stringify(h.type)}`);
  const tNs = h.t_ns;
  if (!isNsString(tNs)) throw new FrameError('psd header: "t_ns" must be a decimal string');
  const nfft = num(h, 'nfft');
  if (!Number.isInteger(nfft) || nfft < 2 || nfft > 131072) throw new FrameError(`psd header: nfft ${nfft} out of range`);
  const hasBaseline = h.has_baseline === true;
  const expected = 4 * nfft * (hasBaseline ? 2 : 1);
  if (payload.byteLength !== expected) {
    throw new FrameError(`psd payload is ${payload.byteLength} bytes, expected ${expected} (nfft ${nfft}${hasBaseline ? ' with baseline' : ''})`);
  }
  const header: PsdHeader = {
    type: 'psd',
    channel_id: str(h, 'channel_id'),
    monitor_id: str(h, 'monitor_id'),
    t_ns: tNs,
    center_hz: num(h, 'center_hz'),
    span_hz: num(h, 'span_hz'),
    nfft,
    seq: typeof h.seq === 'number' ? h.seq : 0,
    has_baseline: hasBaseline,
    detections: detections(h.detections),
  };
  const psd = readFloats(payload, 0, nfft);
  const baseline = hasBaseline ? readFloats(payload, 4 * nfft, nfft) : null;
  return { header, psd, baseline };
}

/** Encode a console spectrum frame (used by tests and the mock server's TS twin). */
export function encodePsdFrame(header: Omit<PsdHeader, 'type'>, psd: Float32Array, baseline: Float32Array | null, pad = true): ArrayBuffer {
  const json = JSON.stringify({ type: 'psd', ...header, has_baseline: baseline !== null });
  let headerBytes = new TextEncoder().encode(json);
  if (pad) {
    const padLen = (8 - ((8 + headerBytes.length) % 8)) % 8;
    headerBytes = new TextEncoder().encode(json + ' '.repeat(padLen));
  }
  const n = psd.length * (baseline ? 2 : 1);
  const buf = new ArrayBuffer(8 + headerBytes.length + 4 * n);
  const bytes = new Uint8Array(buf);
  bytes.set([0x50, 0x4c, 0x42, 0x31], 0);
  new DataView(buf).setUint32(4, headerBytes.length, true);
  bytes.set(headerBytes, 8);
  const dv = new DataView(buf, 8 + headerBytes.length);
  psd.forEach((v, i) => dv.setFloat32(4 * i, v, true));
  if (baseline) baseline.forEach((v, i) => dv.setFloat32(4 * (psd.length + i), v, true));
  return buf;
}
