// Field components for the configuration editor. Every field is addressed by its path in
// the SystemConfig document (the same dotted path the server uses in field errors), so a
// 422 or a validate result lands on the field it is about.
import { createContext, useContext, useEffect, useId, useState, type ReactNode } from 'react';
import type { FieldProblem } from '../api/types.gen';
import { hzToMhzInput, parseMhzToHz } from '../format/units';

export type Path = Array<string | number>;
export const dotted = (p: Path) => p.join('.');

export function getIn(obj: unknown, path: Path): unknown {
  let cur: unknown = obj;
  for (const k of path) {
    if (cur === null || cur === undefined || typeof cur !== 'object') return undefined;
    cur = (cur as Record<string | number, unknown>)[k];
  }
  return cur;
}

/** Immutable set; `undefined` deletes the key (optional fields left unset). */
export function setIn<T>(obj: T, path: Path, value: unknown): T {
  if (!path.length) return value as T;
  const [k, ...rest] = path;
  const base: unknown = obj ?? (typeof k === 'number' ? [] : {});
  if (Array.isArray(base)) {
    const copy = [...base];
    copy[k as number] = setIn(copy[k as number], rest, value);
    return copy as T;
  }
  const copy: Record<string, unknown> = { ...(base as Record<string, unknown>) };
  const next = setIn(copy[k as string], rest, value);
  if (next === undefined) delete copy[k as string];
  else copy[k as string] = next;
  return copy as T;
}

/** Dotted paths where two documents differ (for the version-conflict summary). */
export function diffPaths(a: unknown, b: unknown, prefix: Path = [], out: string[] = [], limit = 200): string[] {
  if (out.length >= limit) return out;
  if (a === b) return out;
  const ao = a !== null && typeof a === 'object';
  const bo = b !== null && typeof b === 'object';
  if (!ao || !bo || Array.isArray(a) !== Array.isArray(b)) {
    out.push(dotted(prefix) || '(root)');
    return out;
  }
  const keys = new Set([...Object.keys(a as object), ...Object.keys(b as object)]);
  for (const k of keys) {
    const key: string | number = Array.isArray(a) ? Number(k) : k;
    diffPaths((a as Record<string, unknown>)[k], (b as Record<string, unknown>)[k], [...prefix, key], out, limit);
  }
  return out;
}

export interface EditorCtx {
  doc: unknown;
  update: (path: Path, value: unknown) => void;
  errors: FieldProblem[];
  warnings: FieldProblem[];
}

export const EditorContext = createContext<EditorCtx>({ doc: null, update: () => {}, errors: [], warnings: [] });

export function useField(path: Path) {
  const ctx = useContext(EditorContext);
  const key = dotted(path);
  const err = ctx.errors.filter((e) => e.loc === key);
  const warn = ctx.warnings.filter((e) => e.loc === key);
  return { value: getIn(ctx.doc, path), set: (v: unknown) => ctx.update(path, v), err, warn, id: `cfg-${key}` };
}

/** Problems located on a container (a satellite, a channel, the region) rather than a leaf field. */
export function ContainerProblems({ path, deep = false, hideWarnings = false }: { path: Path; deep?: boolean; hideWarnings?: boolean }) {
  const ctx = useContext(EditorContext);
  const key = dotted(path);
  const errs = ctx.errors.filter((e) => e.loc === key || (deep && e.loc.startsWith(`${key}.`)));
  const warns = hideWarnings ? [] : ctx.warnings.filter((e) => e.loc === key);
  if (!errs.length && !warns.length) return null;
  return (
    <div>
      {errs.map((e, i) => (
        <div key={`e${i}`} className="notice critical" role="alert" id={`cfg-${e.loc}`} tabIndex={-1}>
          <span className="err-text">{e.msg}</span>
        </div>
      ))}
      {warns.map((w, i) => (
        <div key={`w${i}`} className="notice caution">
          {w.msg}
        </div>
      ))}
    </div>
  );
}

function Shell({ id, label, unit, err, warn, hint, children, info }: { id: string; label: string; unit?: string; err: FieldProblem[]; warn: FieldProblem[]; hint?: ReactNode; children: ReactNode; info?: ReactNode }) {
  return (
    <div className="field">
      {label ? (
        <label htmlFor={id}>
          {label} {unit ? <span className="unit">{unit}</span> : null}
          {info}
        </label>
      ) : null}
      {children}
      {err.map((e, i) => (
        <span key={i} className="err" id={`${id}-err-${i}`}>
          {e.msg}
        </span>
      ))}
      {warn.map((w, i) => (
        <span key={`w${i}`} className="hint">
          {w.msg}
        </span>
      ))}
      {hint && !err.length ? <span className="hint">{hint}</span> : null}
    </div>
  );
}

export function TextF({ path, label, mono, hint, optional, wide, aria }: { path: Path; label: string; mono?: boolean; hint?: ReactNode; optional?: boolean; wide?: boolean; aria?: string }) {
  const f = useField(path);
  return (
    <Shell id={f.id} label={label} err={f.err} warn={f.warn} hint={hint}>
      <input
        id={f.id}
        className={mono ? 'mono' : undefined}
        value={typeof f.value === 'string' ? f.value : f.value === undefined || f.value === null ? '' : String(f.value)}
        onChange={(e) => f.set(optional && e.target.value === '' ? undefined : e.target.value)}
        aria-label={label ? undefined : aria}
        aria-invalid={f.err.length ? true : undefined}
        aria-describedby={f.err.length ? `${f.id}-err-0` : undefined}
        style={wide ? { width: '100%' } : undefined}
      />
    </Shell>
  );
}

/**
 * Number in a display unit: the document holds value * scale (kHz shown, Hz stored: scale 1000).
 * Text that does not parse is kept as typed and sent as is, so the server names the problem.
 */
export function NumF({ path, label, unit, scale = 1, optional, hint, step, info, integer, aria, placeholder }: { path: Path; label: string; unit?: string; scale?: number; optional?: boolean; hint?: ReactNode; step?: string; info?: ReactNode; integer?: boolean; aria?: string; placeholder?: string }) {
  const f = useField(path);
  const fmt = (v: unknown) => (typeof v === 'number' ? String(Number((v / scale).toPrecision(12))) : v === undefined || v === null ? '' : String(v));
  const [text, setText] = useState(fmt(f.value));
  const [focused, setFocused] = useState(false);
  useEffect(() => {
    if (!focused) setText(fmt(f.value));
  }, [f.value, focused]);
  return (
    <Shell id={f.id} label={label} unit={unit} err={f.err} warn={f.warn} hint={hint} info={info}>
      <input
        id={f.id}
        className="mono"
        inputMode={integer ? 'numeric' : 'decimal'}
        value={text}
        placeholder={placeholder}
        step={step}
        onFocus={() => setFocused(true)}
        onBlur={() => setFocused(false)}
        onChange={(e) => {
          const t = e.target.value;
          setText(t);
          const trimmed = t.trim().replace('−', '-');
          if (trimmed === '' && optional) return f.set(undefined);
          const n = Number(trimmed);
          f.set(trimmed !== '' && Number.isFinite(n) ? (integer ? n : n * scale) : t);
        }}
        aria-label={label ? undefined : aria}
        aria-invalid={f.err.length ? true : undefined}
        aria-describedby={f.err.length ? `${f.id}-err-0` : undefined}
        style={{ textAlign: 'right' }}
      />
    </Shell>
  );
}

/** Frequency typed in MHz, stored in Hz exactly (301.0125 -> 301012500). */
export function FreqF({ path, label, hint, aria }: { path: Path; label: string; hint?: ReactNode; aria?: string }) {
  const f = useField(path);
  const fmt = (v: unknown) => (typeof v === 'number' ? hzToMhzInput(v) : v === undefined || v === null ? '' : String(v));
  const [text, setText] = useState(fmt(f.value));
  const [focused, setFocused] = useState(false);
  useEffect(() => {
    if (!focused) setText(fmt(f.value));
  }, [f.value, focused]);
  return (
    <Shell id={f.id} label={label} unit={label ? 'MHz' : undefined} err={f.err} warn={f.warn} hint={hint}>
      <input
        id={f.id}
        className="mono"
        inputMode="decimal"
        value={text}
        onFocus={() => setFocused(true)}
        onBlur={() => setFocused(false)}
        onChange={(e) => {
          setText(e.target.value);
          const hz = parseMhzToHz(e.target.value);
          f.set(hz === null ? e.target.value : hz);
        }}
        aria-label={label ? undefined : aria}
        aria-invalid={f.err.length ? true : undefined}
        style={{ textAlign: 'right' }}
      />
    </Shell>
  );
}

export function BoolF({ path, label }: { path: Path; label: string }) {
  const f = useField(path);
  return (
    <div className="field">
      <label>
        <input id={f.id} type="checkbox" checked={f.value === true} onChange={(e) => f.set(e.target.checked)} /> {label}
      </label>
      {f.err.map((e, i) => (
        <span key={i} className="err">
          {e.msg}
        </span>
      ))}
    </div>
  );
}

export function SelectF({ path, label, options, hint }: { path: Path; label: string; options: Array<[string, string]>; hint?: ReactNode }) {
  const f = useField(path);
  const id = useId();
  const v = typeof f.value === 'string' ? f.value : '';
  const known = options.some(([o]) => o === v);
  return (
    <Shell id={f.id || id} label={label} err={f.err} warn={f.warn} hint={hint}>
      <select id={f.id || id} value={v} onChange={(e) => f.set(e.target.value)} aria-invalid={f.err.length ? true : undefined}>
        {!known ? <option value={v}>{v || '(none)'}</option> : null}
        {options.map(([o, l]) => (
          <option key={o} value={o}>
            {l}
          </option>
        ))}
      </select>
    </Shell>
  );
}

/** Three numbers (RIC sigmas, vectors). */
export function Vec3F({ path, label, unit }: { path: Path; label: string; unit?: string }) {
  const parts = ['R', 'I', 'C'];
  return (
    <div className="field">
      <span className="label">
        {label} {unit ? <span className="unit">{unit}</span> : null}
      </span>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 4 }}>
        {parts.map((p, i) => (
          <NumF key={p} path={[...path, i]} label={p} />
        ))}
      </div>
    </div>
  );
}
