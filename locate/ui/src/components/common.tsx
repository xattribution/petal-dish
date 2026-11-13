// Small shared pieces: Astro status symbol, the (i) tip, SIMULATION tag, empty states.
import { useEffect, useId, useRef, useState, type ReactNode } from 'react';
import { LEVEL_WORD, type Level } from '../domain/status';

/** Astro status: color + shape + word. Shapes: ring, ring with dot, circle, diamond, triangle, square. */
export function StatusSymbol({ level, size = 12, title }: { level: Level; size?: number; title?: string }) {
  const s = size;
  const c = s / 2;
  let shape: ReactNode;
  switch (level) {
    case 'off':
      shape = (
        <>
          <circle cx={c} cy={c} r={c - 1} className="sym-off" strokeWidth={1} />
          <circle cx={c} cy={c} r={c * 0.45} className="sym-hole" />
        </>
      );
      break;
    case 'standby':
      shape = (
        <>
          <circle cx={c} cy={c} r={c - 1} className="sym-standby" strokeWidth={1} />
          <circle cx={c} cy={c} r={c * 0.55} className="sym-hole" />
          <circle cx={c} cy={c} r={c * 0.28} className="sym-standby" strokeWidth={0} />
        </>
      );
      break;
    case 'normal':
      shape = <circle cx={c} cy={c} r={c - 1} className="sym-normal" strokeWidth={1} />;
      break;
    case 'caution':
      shape = <polygon points={`${c},0.5 ${s - 0.5},${c} ${c},${s - 0.5} 0.5,${c}`} className="sym-caution" strokeWidth={1} />;
      break;
    case 'serious':
      shape = <polygon points={`${c},0.8 ${s - 0.5},${s - 1} 0.5,${s - 1}`} className="sym-serious" strokeWidth={1} />;
      break;
    case 'critical':
      shape = <rect x={1} y={1} width={s - 2} height={s - 2} className="sym-critical" strokeWidth={1} />;
      break;
  }
  return (
    <svg width={s} height={s} viewBox={`0 0 ${s} ${s}`} role="img" aria-label={title ?? LEVEL_WORD[level]}>
      {shape}
    </svg>
  );
}

export function Status({ level, word, size }: { level: Level; word?: string; size?: number }) {
  return (
    <span className="status">
      <StatusSymbol level={level} size={size} />
      <span className="word">{word ?? LEVEL_WORD[level]}</span>
    </span>
  );
}

/** The (i) pattern: help on hover and on keyboard focus, Esc dismisses (WCAG 1.4.13). */
export function Info({ label, children }: { label: string; children: ReactNode }) {
  const id = useId();
  const ref = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && ref.current) ref.current.dataset.dismissed = '';
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);
  const undismiss = () => {
    if (ref.current) delete ref.current.dataset.dismissed;
  };
  return (
    <span className="tipwrap">
      <button
        ref={ref}
        type="button"
        className="info"
        aria-label={`About ${label}`}
        aria-describedby={id}
        onFocus={undismiss}
        onPointerEnter={undismiss}
      >
        i
      </button>
      <span role="tooltip" id={id} className="tip">
        {children}
      </span>
    </span>
  );
}

export function SimTag({ text = 'SIMULATION' }: { text?: string }) {
  return <span className="simtag">{text}</span>;
}

export function Empty({ title, children, action }: { title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="empty" role="status">
      <h2>{title}</h2>
      {children ? <p>{children}</p> : null}
      {action}
    </div>
  );
}

/** Loading text that appears only after 250 ms, so quick answers do not flash. */
export function Loading({ what }: { what: string }) {
  const [show, setShow] = useState(false);
  useEffect(() => {
    const t = setTimeout(() => setShow(true), 250);
    return () => clearTimeout(t);
  }, []);
  return <div className="loading" aria-live="polite">{show ? `Loading ${what}…` : ' '}</div>;
}

export function ErrorNote({ error, retry }: { error: string; retry?: () => void }) {
  return (
    <div className="notice critical" role="alert">
      <span className="err-text">{error}</span>
      {retry ? (
        <>
          {' '}
          <button type="button" className="linkbtn" onClick={retry}>
            Retry
          </button>
        </>
      ) : null}
    </div>
  );
}

/** Re-render every `ms` (clocks, ages). */
export function useTick(ms: number): number {
  const [t, setT] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setT(Date.now()), ms);
    return () => clearInterval(id);
  }, [ms]);
  return t;
}
