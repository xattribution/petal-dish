// Event dossier: what the operator must decide first (verdict and advisory), the evidence
// for it (the evidence matrix), where the emitter is and how sure the system is (fix,
// ellipse, ambiguity, lines of position, measurements, correlation surface, history).
import { memo, useEffect, useMemo, useState } from 'react';
import type { Event, EvidenceRow, LopCollection, MeasurementRow, NodeStatus, SolutionOut, SystemConfig, TaskingStatus } from '../api/types.gen';
import { ErrorNote, Info, Loading, SimTag, Status, StatusSymbol, useTick } from '../components/common';
import { Heatmap, LinePlot } from '../components/plots';
import { candidates, isInconsistent, isNotFix, notFixText, received, semiAxes, splitHistory, widthText } from '../domain/event';
import { eventLevel, isActive, STATE_WORD, VERDICT_WORD } from '../domain/status';
import { DATUM, distanceM, formatLat, formatLatLon, formatLon, formatMgrs } from '../format/geo';
import { DASH, db, deg, fixed, hz3, km, mhz, micros, millihertz, percent, plural } from '../format/units';
import { DOMINANT_WORD, MapView, type MapData, type MapFocus, type MapSolution } from '../map/MapView';
import { href, navigate } from '../router';
import { configResource, subpointsResource } from '../state/resources';
import { api, freshness, loadEvent, serverNowMs, useLive } from '../state/store';
import { dtgNs, dtgSecondsNs, formatAge, zuluClock } from '../time/dtg';
import { nsDiffSeconds, nsToDate, nsToIso, nsToMs } from '../time/ns';
import { useLayers } from './Watch';

const EV_WORD: Record<EvidenceRow['state'], string> = {
  seen: 'Seen',
  not_seen_capable: 'Not seen',
  not_capable: 'Cannot see',
  stale: 'Stale',
};

function EvGlyph({ state }: { state: EvidenceRow['state'] }) {
  const cls = state === 'seen' ? 'ev-seen' : state === 'not_seen_capable' ? 'ev-nsc' : state === 'not_capable' ? 'ev-nc' : 'ev-stale';
  return (
    <svg width={8} height={14} viewBox="0 0 8 14" aria-hidden="true">
      {state === 'not_capable' ? <rect x={1} y={6} width={6} height={2} className={cls} /> : <rect x={1.5} y={1} width={5} height={12} className={cls} />}
    </svg>
  );
}

/** node: its newest status; sinceStatusS: seconds since that status arrived (frame ages grow by it). */
function evidenceNumbers(r: EvidenceRow, nowMs: number, node: NodeStatus | undefined, sinceStatusS = 0): string {
  switch (r.state) {
    case 'seen':
      return `SNR ${db(r.snr_db)} dB, C/N0 ${db(r.cn0_dbhz)} dBHz`;
    case 'not_seen_capable': {
      const base = r.predicted_margin_db !== null ? `expected +${db(r.predicted_margin_db)} dB margin` : 'expected to see it';
      // a channel that saw the event earlier and no longer does
      return r.last_seen_ns ? `${base}, last saw it ${formatAge(Math.max(0, (nowMs - nsToMs(r.last_seen_ns)) / 1000))} ago` : base;
    }
    case 'not_capable':
      // null margin: no sighting through this satellite to predict from (MATH.md section 7)
      return r.predicted_margin_db !== null ? `margin ${db(r.predicted_margin_db)} dB` : 'not seen through this satellite anywhere';
    case 'stale': {
      // how old the evidence is: the channel's newest spectrum frame (status), and when it last saw the signal
      const parts: string[] = [];
      const reported = node?.channels.find((c) => c.channel_id === r.channel_id)?.last_frame_age_s;
      // the frame age was true when the status arrived; it has grown since (UX-8)
      const age = reported === null || reported === undefined ? reported : reported + Math.max(0, sinceStatusS);
      if (node && !node.online) parts.push('node offline');
      if (age !== null && age !== undefined) parts.push(`no frame for ${formatAge(age)}`);
      else if (!node || node.online) parts.push('no recent frames');
      const detAge = r.last_seen_ns ? (nowMs - nsToMs(r.last_seen_ns)) / 1000 : null;
      if (detAge !== null && (age === null || age === undefined || Math.abs(detAge - age) > 5)) parts.push(`last detection ${formatAge(detAge)} ago`);
      return parts.join(', ');
    }
  }
}

/** Signature element: sites x satellites, one cell per receive channel. */
function EvidenceMatrix({ ev, cfg }: { ev: Event; cfg: SystemConfig | null }) {
  useTick(1000);
  const nowMs = serverNowMs();
  const nodes = useLive((s) => s.status?.nodes ?? null);
  const statusAt = useLive((s) => s.status?.receivedAt ?? null);
  const sinceStatusS = statusAt === null ? 0 : (Date.now() - statusAt) / 1000;
  const nodeOf = (site: string) => nodes?.find((n) => n.site_id === site);
  const sats = useMemo(() => {
    const s = new Set<string>(ev.satellite_ids);
    for (const r of ev.evidence) if (r.sat_id) s.add(r.sat_id);
    return [...s];
  }, [ev]);
  const siteOrder = cfg ? cfg.sites.map((s) => s.id) : [...new Set(ev.evidence.map((r) => r.site_id))];
  const sites = siteOrder.filter((id) => ev.evidence.some((r) => r.site_id === id) || cfg?.sites.some((s) => s.id === id));
  const siteName = (id: string) => cfg?.sites.find((s) => s.id === id)?.name.replace(/\s*\(.*\)$/, '') ?? id;
  const counts = { seen: 0, not_seen_capable: 0, not_capable: 0, stale: 0 };
  for (const r of ev.evidence) counts[r.state] += 1;
  const noSat = ev.evidence.filter((r) => !r.sat_id);
  if (!ev.evidence.length) return <p className="ink2">No channel watches this range, so there is no evidence table.</p>;
  return (
    <>
      <div className="tablewrap">
        <table className="matrix">
          <caption className="sr-only">Evidence by site (rows) and satellite (columns)</caption>
          <thead>
            <tr>
              <th scope="col">Site</th>
              {sats.map((s) => (
                <th key={s} scope="col" className="mono">
                  {s}
                </th>
              ))}
              {noSat.length ? <th scope="col">Own receiver</th> : null}
            </tr>
          </thead>
          <tbody>
            {sites.map((site) => (
              <tr key={site}>
                <th scope="row">{siteName(site)}</th>
                {sats.map((sat) => {
                  const rows = ev.evidence.filter((r) => r.site_id === site && r.sat_id === sat);
                  if (!rows.length) {
                    return (
                      <td key={sat} className="none">
                        no channel
                      </td>
                    );
                  }
                  return (
                    <td key={sat} className={rows.every((r) => r.state === 'stale') ? 'stale' : undefined}>
                      {rows.map((r) => (
                        <div className="ev" key={r.channel_id} title={r.channel_id}>
                          <EvGlyph state={r.state} />
                          <span className="w">{EV_WORD[r.state]}</span>
                          <span className="n">{evidenceNumbers(r, nowMs, nodeOf(r.site_id), sinceStatusS)}</span>
                        </div>
                      ))}
                    </td>
                  );
                })}
                {noSat.length && !noSat.some((r) => r.site_id === site) ? (
                  <td className="none">no window on this frequency</td>
                ) : noSat.length ? (
                  <td>
                    {noSat
                      .filter((r) => r.site_id === site)
                      .map((r) => (
                        <div className="ev" key={r.channel_id}>
                          <EvGlyph state={r.state} />
                          <span className="w">{EV_WORD[r.state]}</span>
                          <span className="n">{evidenceNumbers(r, nowMs, nodeOf(r.site_id), sinceStatusS)}</span>
                        </div>
                      ))}
                  </td>
                ) : null}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="ink2" style={{ marginTop: 6 }}>
        {plural(counts.seen, 'channel')} {counts.seen === 1 ? 'sees' : 'see'} it, {counts.not_seen_capable} could see it and {counts.not_seen_capable === 1 ? 'does' : 'do'} not, {counts.not_capable} cannot see it, {counts.stale} stale.
      </p>
    </>
  );
}

/** "naha: muos-pac − uhf-172e" when both ends are at one site (common clock), else "muos-io@manila − uhf-172e@busan". */
function pairText(m: { pair: { a: { sat_id: string; site_id: string }; b: { sat_id: string; site_id: string } } }): string {
  const { a, b } = m.pair;
  if (a.site_id === b.site_id) return `${a.site_id}: ${a.sat_id} − ${b.sat_id}`;
  return `${a.sat_id}@${a.site_id} − ${b.sat_id}@${b.site_id}`;
}

const zuluTime = (ns: string) => zuluClock(nsToDate(ns)).slice(0, 8);

// Short words for the reasons tasking gives a row it did not use (the full text is in the
// key under the table). Server codes from pipeline/tasking.py; anything else is shown as is.
const REASON_WORD: Record<string, string> = {
  not_detected: 'not detected',
  ambiguous: 'ambiguous',
  delay_doppler_coupled: 'delay–Doppler coupled',
  narrowband: 'too narrow to time',
  outside_window: 'peak outside window',
  mirror_ambiguous: 'mirror copy',
  clock_unlocked: 'clock unlocked',
  rate_mismatch: 'rate mismatch',
  overflow: 'overflow',
  unconfirmed: 'unconfirmed',
  inconsistent: 'inconsistent',
  window_exceeds_capture: 'window too wide',
  capture_failed: 'capture failed',
  'set aside': 'set aside',
  'correlation error': 'correlation error',
  // core.caf reason strings, should a row carry one
  'below threshold': 'not detected',
  'delay-doppler coupled': 'delay–Doppler coupled',
  'peak outside window': 'peak outside window',
};

/** What each reason means, for the key under the table. */
const REASON_TEXT: Record<string, string> = {
  not_detected: 'the copy relayed by the other satellite was not found above the detection threshold',
  ambiguous: 'several correlation peaks of almost equal height (a periodic waveform matches itself one period later)',
  delay_doppler_coupled: 'delay and Doppler cannot be told apart (a single sweep)',
  narrowband: 'the signal is too narrow for a time difference to be measured; its frequency difference may still be used',
  outside_window: 'the strongest peak lies outside the window the geometry allows',
  mirror_ambiguous: 'a co-channel satellite’s copy correlates almost as strongly',
  clock_unlocked: 'a node clock was unlocked, so its timing cannot be trusted for a time difference',
  rate_mismatch: 'the nodes captured at different sample rates',
  overflow: 'the receiver overflowed during the capture',
  window_exceeds_capture: 'the search window is wider than the capture can cover',
  capture_failed: 'a node could not deliver the capture',
  inconsistent: 'does not repeat the time and frequency difference this pair confirmed earlier',
};

/** "unconfirmed: awaiting ..." -> "unconfirmed"; "capture_failed: too_late: ..." -> "capture_failed". */
function reasonKey(reason: string | null): string {
  if (!reason) return '';
  if (reason.startsWith('inconsistent')) return 'inconsistent';
  if (reason.startsWith('tdoa:')) return reason; // "tdoa: x; fdoa: y" is already specific
  return reason.split(':')[0]?.trim() ?? reason;
}

function reasonWord(reason: string | null): string {
  if (!reason) return 'not used';
  const key = reasonKey(reason);
  return REASON_WORD[key] ?? reason;
}

/** Value and sigma of a row in its own unit: TDOA in µs (ns resolution), FDOA in Hz (mHz). */
function valueText(r: MeasurementRow, v: number | null | undefined): string {
  if (v === null || v === undefined) return '';
  return r.kind === 'tdoa' ? `${micros(v)} µs` : `${hz3(v)} Hz`;
}

function sigmaText(r: MeasurementRow): string {
  if (r.sigma === null || r.sigma === undefined) return '';
  return r.kind === 'tdoa' ? `${micros(r.sigma)} µs` : `${millihertz(r.sigma)} mHz`;
}

/** Rows the table renders before "Show all" (an event keeps up to 1000; each update re-renders them). */
const TABLE_ROWS = 120;

function MeasurementTable({ rows }: { rows: MeasurementRow[] }) {
  const [usedOnly, setUsedOnly] = useState(false);
  const [all, setAll] = useState(false);
  const sorted = useMemo(() => [...rows].sort((a, b) => (BigInt(b.t_ns) > BigInt(a.t_ns) ? 1 : BigInt(b.t_ns) < BigInt(a.t_ns) ? -1 : a.kind.localeCompare(b.kind))), [rows]);
  const matching = usedOnly ? sorted.filter((r) => r.used) : sorted;
  const shown = all ? matching : matching.slice(0, TABLE_ROWS);
  const used = rows.filter((r) => r.used).length;
  const calibrated = rows.filter((r) => r.used && r.calibrated);
  const refs = [...new Set(calibrated.map((r) => r.reference_carrier_id).filter((x): x is string => !!x))];
  // one reference (the usual case) is named above the table; a column only when they differ
  const refColumn = new Set(rows.filter((r) => r.calibrated).map((r) => r.reference_carrier_id ?? '')).size > 1;
  // why the other rows were not used: each full reason once, with its count
  const why = useMemo(() => {
    const m = new Map<string, number>();
    for (const r of rows) if (!r.used) m.set(r.reason ?? 'not used', (m.get(r.reason ?? 'not used') ?? 0) + 1);
    return [...m.entries()].sort((a, b) => b[1] - a[1]);
  }, [rows]);
  return (
    <>
      <div style={{ display: 'flex', gap: 14, alignItems: 'center', marginBottom: 6, flexWrap: 'wrap' }}>
        <span className="ink2">
          {used} of {rows.length} used by the solver
          {used ? `, ${calibrated.length} of them calibrated${refs.length ? ` against ${refs.join(', ')}` : ''}` : ''}
        </span>
        <label>
          <input type="checkbox" checked={usedOnly} onChange={(e) => setUsedOnly(e.target.checked)} /> Used only
        </label>
        {matching.length > TABLE_ROWS ? (
          <button type="button" className="btn small" onClick={() => setAll(!all)}>
            {all ? `Newest ${TABLE_ROWS} only` : `Show all ${matching.length} rows`}
          </button>
        ) : null}
      </div>
      <div className="tablewrap" style={{ maxHeight: 340 }}>
        <table className="grid compact meas">
          <thead>
            <tr>
              <th>Time (Z)</th>
              <th>Pair (A − B)</th>
              <th>Kind</th>
              <th className="num" title="Measured difference A − B, after the calibration correction when calibrated">
                Value
              </th>
              <th className="num" title="One-sigma measurement noise of this row">
                <span className="unit">σ</span>
              </th>
              <th className="num" title="Reference-carrier correction already subtracted from the value (raw = value + correction)">
                Correction
              </th>
              {refColumn ? <th title="Reference carrier the correction was measured on">Reference</th> : null}
              <th className="num" title="Correlation (post-integration) SNR">
                SNR <span className="unit">dB</span>
              </th>
              <th className="num" title="Whitened residual against the current solution">
                Resid. <span className="unit">σ</span>
              </th>
              <th>Used</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((r, i) => (
              <tr key={`${r.t_ns}-${r.kind}-${pairText(r)}-${i}`} className={r.used ? undefined : 'muted'}>
                <td className="mono">{zuluTime(r.t_ns)}</td>
                <td className="mono">{pairText(r)}</td>
                <td>{r.kind === 'tdoa' ? 'TDOA' : 'FDOA'}</td>
                <td className="num">{r.value === null ? DASH : valueText(r, r.value)}</td>
                <td className="num">{r.value === null ? DASH : sigmaText(r) || DASH}</td>
                <td className="num" title={r.calibrated && r.reference_carrier_id ? `measured on ${r.reference_carrier_id}; raw value = value + correction` : undefined}>
                  {r.calibrated ? (r.correction !== null && r.correction !== undefined ? valueText(r, r.correction) : 'yes') : ''}
                </td>
                {refColumn ? <td className="mono">{r.calibrated ? (r.reference_carrier_id ?? '') : ''}</td> : null}
                <td className="num">{db(r.snr_db)}</td>
                <td className="num">{fixed(r.residual, 2)}</td>
                <td title={r.used ? undefined : (r.reason ?? undefined)}>{r.used ? 'yes' : `no: ${reasonWord(r.reason)}`}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {why.length ? (
        <dl className="whykey" aria-label="Why rows were not used">
          {why.map(([reason, n]) => {
            const key = reasonKey(reason);
            const word = REASON_WORD[key];
            const rest = reason.includes(':') ? reason.slice(reason.indexOf(':') + 1).trim() : '';
            const explain = REASON_TEXT[key] ?? (rest || null);
            return (
              <div key={reason}>
                <dt>
                  {word ?? reason} <span className="ink2">({plural(n, 'row')})</span>
                </dt>
                {explain && word ? <dd>{explain}{rest && REASON_TEXT[key] ? ` (${rest})` : ''}</dd> : null}
              </div>
            );
          })}
        </dl>
      ) : null}
    </>
  );
}

const RESOLVED_TEXT: Record<SolutionOut['ambiguity_resolved_by'], string> = {
  none: 'No second position fits the measurements.',
  region: 'A second position (the mirror) fits as well, but lies outside the region of interest or below the elevation mask, so it was ruled out.',
  measurements: 'A second position was found but fits the measurements clearly worse.',
  unresolved: 'These positions fit the measurements equally well; the system cannot choose between them yet. Do not act on any one of them alone.',
};

function shareOf(sol: SolutionOut): { noise: number; consider: number } {
  const tr = (m: number[][]) => (m[0]?.[0] ?? 0) + (m[1]?.[1] ?? 0);
  const total = tr(sol.cov_en_m2) || 1;
  return { noise: tr(sol.cov_noise_en_m2) / total, consider: tr(sol.cov_consider_en_m2) / total };
}

/** Chance that a consistent fit scatters this much: "0.0 %", "3.2 %". */
function pText(p: number | null | undefined): string {
  if (p === null || p === undefined || !Number.isFinite(p)) return '—';
  return p < 0.001 ? 'under 0.1 %' : `${fixed(p * 100, 1)} %`;
}

/** Why no position: the most specific reason first (UX-4: a signal that cannot be located says so). */
function noPositionReason(ev: Event): { title: string; why: string } {
  const t = ev.tasking;
  const setAside = ev.measurements.some((m) => m.reason?.startsWith('set aside')) && !ev.measurements.some((m) => m.used);
  if (ev.verdict === 'local') return { title: 'No position', why: 'Local interference enters at one site, not through the satellites, so it is not located by time and frequency differences.' };
  if (t?.unlocatable_reason) return { title: 'Cannot be located', why: t.unlocatable_reason };
  if (setAside) return { title: 'No position', why: 'The earlier position and its measurements were set aside (see Transitions): the signal was lost for a while or changed, so it is located again from new measurements only.' };
  // R6: a published position the newest measurements no longer admit is withdrawn
  const withdrawn = ev.transitions.at(-1)?.reason?.match(/^position withdrawn: (.+)$/)?.[1];
  if (withdrawn) return { title: 'Position withdrawn', why: `${withdrawn.charAt(0).toUpperCase()}${withdrawn.slice(1)}${/[.?]$/.test(withdrawn) ? '' : '.'} Do not report the earlier position.` };
  if (t?.solve_reason) return { title: 'No position', why: `The last solve gave no position: ${t.solve_reason}.` };
  if (t?.state === 'idle' && t.message) return { title: 'No position', why: t.message };
  if (ev.satellite_ids.length < 2) return { title: 'No position', why: 'Only one satellite relays this range, so there is no satellite pair to correlate.' };
  if (ev.measurements.length === 0) return { title: 'No position', why: 'No correlation result yet.' };
  return { title: 'No position', why: `Not enough usable measurements yet (${ev.measurements.filter((m) => m.used).length} usable; two are needed).` };
}

function Location({ ev }: { ev: Event }) {
  const sol = ev.solution;
  if (!sol) {
    const r = noPositionReason(ev);
    return (
      <div>
        {recheckNote(ev)}
        <h2>{r.title}</h2>
        <p className="ink2">{r.why}</p>
      </div>
    );
  }
  const e = sol.ellipse;
  const mgrs = formatMgrs(sol.lat_deg, sol.lon_deg);
  const share = shareOf(sol);
  const truth = ev.truth;
  const bad = isInconsistent(sol);
  // R4: consistent, but no redundancy or an ellipse too large: shown, never reported
  const notFix = isNotFix(sol);
  const all = candidates(sol);
  return (
    <div>
      {!bad && notFix ? (
        <div className="notice caution" role="note" style={{ marginTop: 0 }}>
          <span className="status">
            <StatusSymbol level="caution" />
            <span className="word">Not a fix</span>
          </span>{' '}
          {notFixText(sol)} The computed position is shown; do not report it until more measurements make it a fix.
        </div>
      ) : null}
      {bad ? (
        // UX-1: the measurements do not fit one stationary transmitter; the position is not a fix
        <div className="notice caution" role="note" style={{ marginTop: 0 }}>
          <span className="status">
            <StatusSymbol level="caution" />
            <span className="word">Not a fix</span>
          </span>{' '}
          The measurements do not fit one stationary transmitter (reduced {'χ²'} {fixed(sol.reduced_chi2, 1)}, chance of this scatter {pText(sol.fit_p_value)}): rows of two
          transmitters, a moving one, or a clock or ephemeris fault. The computed position is shown with its ellipse enlarged {fixed(sol.ellipse_scale ?? 1, 1)} times; do not report it.
        </div>
      ) : null}
      {recheckNote(ev)}
      {all.length ? (
        // UX-6: an unresolved ambiguity lists every position that fits, none of them as "the" fix
        <>
          <h2>One of {all.length} positions</h2>
          <table className="grid compact candidates" style={{ marginBottom: 8 }}>
            <thead>
              <tr>
                <th className="num">#</th>
                <th>Position ({DATUM})</th>
                <th className="num">
                  95 % semi-axes <span className="unit">km</span>
                </th>
                <th className="num">{'Δχ²'}</th>
              </tr>
            </thead>
            <tbody>
              {all.map((c, i) => (
                <tr key={i}>
                  <td className="num">{i + 1}</td>
                  <td className="mono">{formatLatLon(c.lat, c.lon, 4)}</td>
                  <td className="num">
                    {km(c.semiMajorM)} {'×'} {km(c.semiMinorM)}
                  </td>
                  <td className="num">{fixed(c.dChi2, 2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      ) : (
        <>
          <div className={`fixline mono${notFix ? ' ink2' : ''}`}>
            {notFix ? <span style={{ fontSize: 12 }}>Computed, not a fix: </span> : null}
            {formatLatLon(sol.lat_deg, sol.lon_deg, 4)} <span className="ink2" style={{ fontSize: 12 }}>{DATUM}</span>
          </div>
          <div className="mono" style={{ marginBottom: 8 }}>
            MGRS {mgrs ?? 'not defined at this latitude (UPS zone)'}
          </div>
        </>
      )}
      <dl className="facts">
        <dt>95 % ellipse</dt>
        <dd>
          <span className="mono">{semiAxes(e.semi_major_m, e.semi_minor_m)}</span> semi-axes, major axis {deg(e.azimuth_deg, 0)}
          {'°'} true{bad ? `, enlarged ${fixed(sol.ellipse_scale ?? 1, 1)} times` : ''}
          {all.length ? ' (position 1)' : ''}
        </dd>
        <dt>CEP50</dt>
        <dd className="mono">{km(sol.cep50_m)} km</dd>
        <dt>
          Dominant error{' '}
          <Info label="dominant error">
            The largest contributor to the position variance. Systematic terms (ephemeris, clocks, oscillators, TDOA bias, reference, ionosphere, troposphere) do not shrink
            with more captures; noise does. A reference emitter on the same pair removes most of the ephemeris term.
          </Info>
        </dt>
        <dd>
          {DOMINANT_WORD[sol.dominant_error] ?? sol.dominant_error}: noise {percent(share.noise)}, systematic {percent(share.consider)} of the variance
        </dd>
        <dt>Fit</dt>
        <dd>
          {sol.n_measurements} measurements, {'χ²'} {fixed(sol.chi2, 1)} on {sol.dof} degrees of freedom
          {sol.reduced_chi2 !== null ? ` (reduced ${fixed(sol.reduced_chi2, 2)})` : ''}
          {bad ? ': inconsistent' : notFix && sol.dof < 1 ? ': no redundancy' : ''}
        </dd>
        <dt>Solved</dt>
        <dd className="mono">{ev.solution_t_ns ? dtgSecondsNs(ev.solution_t_ns) : '—'}</dd>
        <dt>Ambiguity</dt>
        <dd>
          {RESOLVED_TEXT[sol.ambiguity_resolved_by]}
          {all.length
            ? null
            : sol.ambiguities.map((a, i) => (
                <div key={i} className="mono" style={{ marginTop: 2 }}>
                  Mirror {i + 1}: {formatLatLon(a.lat_deg, a.lon_deg, 3)}, {km(distanceM(sol.lat_deg, sol.lon_deg, a.lat_deg, a.lon_deg))} km away, {'Δχ²'} {fixed(a.chi2 - sol.chi2, 2)}
                  , 95 % semi-axes {semiAxes(a.ellipse.semi_major_m, a.ellipse.semi_minor_m)} (dashed on the map)
                </div>
              ))}
          {all.length ? <div style={{ marginTop: 2 }}>Position 1 is drawn solid on the map, the others dashed.</div> : null}
        </dd>
      </dl>
      {truth ? (
        <div className="notice" style={{ marginTop: 10 }}>
          <SimTag /> Truth {truth.emitter_id}: <span className="mono">{formatLatLon(truth.lat_deg, truth.lon_deg, 4)}</span>
          {truth.miss_m !== null ? (
            <>
              , miss distance <span className="mono">{km(truth.miss_m)} km</span>, {truth.inside_ellipse ? 'inside' : 'outside'} the 95 % ellipse
            </>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

/** UX-5: the position shown is being checked against new measurements (Event.recheck). */
function recheckNote(ev: Event) {
  if (!ev.recheck) return null;
  return (
    <div className="notice caution" role="note" style={{ marginTop: 0 }}>
      <span className="status">
        <StatusSymbol level="caution" />
        <span className="word">Re-check</span>
      </span>{' '}
      {ev.recheck}
    </div>
  );
}

function EventMap({ ev, cfg }: { ev: Event; cfg: SystemConfig | null }) {
  const subs = subpointsResource.use();
  const [lop, setLop] = useState<LopCollection | null>(null);
  const [lopError, setLopError] = useState<string | null>(null);
  const [layers, setLayers] = useLayers('event', { sites: true, satellites: true, events: true, lops: true, ...(ev.truth ? { truth: true } : {}) });
  useEffect(() => {
    if (!ev.solution && !ev.measurements.some((m) => m.used)) return;
    let live = true;
    api.lop(ev.id).then(
      (fc) => live && (setLop(fc), setLopError(null)),
      (e: Error) => live && setLopError(e.message),
    );
    return () => {
      live = false;
    };
  }, [ev.id, ev.solution_t_ns]);
  const sol = ev.solution;
  // redraw the map only when what it shows changes, not on every event update
  const truthKey = ev.truth ? `${ev.truth.emitter_id}|${ev.truth.lat_deg}|${ev.truth.lon_deg}` : '';
  const data: MapData = useMemo(() => {
    const solutions: MapSolution[] = [];
    if (sol) {
      const bad = isNotFix(sol);
      const unresolved = sol.ambiguity_resolved_by === 'unresolved' && sol.ambiguities.length > 0;
      solutions.push({
        id: 'fix',
        eventId: ev.id,
        lat: sol.lat_deg,
        lon: sol.lon_deg,
        semiMajorM: sol.ellipse.semi_major_m,
        semiMinorM: sol.ellipse.semi_minor_m,
        azimuthDeg: sol.ellipse.azimuth_deg,
        role: bad ? 'unreliable' : 'fix',
        selected: true,
        label: bad ? `${ev.id} (not a fix)` : unresolved ? `${ev.id} position 1` : ev.id,
      });
      sol.ambiguities.forEach((a, i) =>
        solutions.push({ id: `mirror-${i}`, eventId: ev.id, lat: a.lat_deg, lon: a.lon_deg, semiMajorM: a.ellipse.semi_major_m, semiMinorM: a.ellipse.semi_minor_m, azimuthDeg: a.ellipse.azimuth_deg, role: 'mirror', selected: false, label: unresolved ? `position ${i + 2}` : `mirror ${i + 1}` }),
      );
    }
    return {
      sites: cfg?.sites.map((s) => ({ id: s.id, name: s.name, lat: s.lat_deg, lon: s.lon_deg })) ?? [],
      subpoints: subs.data ?? [],
      solutions,
      lops: lop,
      truth: ev.truth ? [{ id: ev.truth.emitter_id, lat: ev.truth.lat_deg, lon: ev.truth.lon_deg, label: 'truth (SIMULATION)' }] : [],
    };
    // keyed on what is drawn (ev and sol change identity on every update)
  }, [ev.id, ev.solution_t_ns, sol?.fit_consistent, sol?.is_fix, truthKey, cfg, subs.data, lop]);
  const focus = useMemo((): MapFocus | null => {
    // the fix with room for its ellipse, every mirror and (in simulation) the truth
    const pts: Array<{ lat: number; lon: number; r: number }> = [];
    if (sol) {
      pts.push({ lat: sol.lat_deg, lon: sol.lon_deg, r: sol.ellipse.semi_major_m * 2.5 });
      for (const a of sol.ambiguities) pts.push({ lat: a.lat_deg, lon: a.lon_deg, r: a.ellipse.semi_major_m * 1.5 });
    }
    if (ev.truth) pts.push({ lat: ev.truth.lat_deg, lon: ev.truth.lon_deg, r: 2000 });
    const first = pts[0];
    if (!first) return null;
    let latMin = Infinity;
    let latMax = -Infinity;
    let lonMin = Infinity;
    let lonMax = -Infinity;
    for (const p of pts) {
      // longitudes unwrapped next to the first point, so a group across the antimeridian stays together
      const lon = first.lon + ((((p.lon - first.lon + 180) % 360) + 360) % 360) - 180;
      const dLat = Math.max(p.r, 1500) / 111_320;
      const dLon = dLat / Math.max(0.05, Math.cos((p.lat * Math.PI) / 180));
      latMin = Math.min(latMin, p.lat - dLat);
      latMax = Math.max(latMax, p.lat + dLat);
      lonMin = Math.min(lonMin, lon - dLon);
      lonMax = Math.max(lonMax, lon + dLon);
    }
    return { latMin, latMax, lonMin, lonMax, key: `${ev.id}-${ev.solution_t_ns ?? ''}` };
  }, [ev.id, ev.solution_t_ns]);
  return (
    <div className="eventmap">
      <MapView
        label={`Map of event ${ev.id}`}
        data={data}
        layers={layers}
        onLayers={setLayers}
        region={cfg?.processing?.region ?? null}
        focus={focus}
        truthLabel="Truth (SIMULATION)"
        ellipseConfidence={sol?.ellipse.confidence}
      >
        {lopError ? <div className="mapnote err-text">Lines of position not available: {lopError}</div> : null}
      </MapView>
    </div>
  );
}

/** Tick decimals that keep neighboring labels distinct over a span of values. */
function spanDigits(values: number[]): number {
  const finite = values.filter(Number.isFinite);
  const span = finite.length ? Math.max(...finite) - Math.min(...finite) : 0;
  return span >= 4 ? 0 : span >= 0.4 ? 1 : 2;
}

function Correlation({ ev }: { ev: Event }) {
  const caf = ev.caf;
  if (!caf) return <p className="ink2">No correlation yet.</p>;
  const us = (s: number) => `${fixed(s * 1e6, 1)}`;
  const candidates = caf.candidates ?? [];
  const delayDigits = spanDigits(caf.delay_cut.db);
  const dopplerDigits = spanDigits(caf.doppler_cut.db);
  return (
    <>
      <p className="ink2">
        <span className="mono">{pairText(caf)}</span> at <span className="mono">{dtgSecondsNs(caf.t_ns)}</span>: peak {caf.peak_tdoa_s !== null ? <span className="mono">{micros(caf.peak_tdoa_s)} µs</span> : 'no delay'},{' '}
        {caf.peak_fdoa_hz !== null ? <span className="mono">{hz3(caf.peak_fdoa_hz)} Hz</span> : 'no Doppler'}, SNR {db(caf.snr_db)} dB, {caf.detected ? 'detected' : 'not detected'}
        {caf.reason ? ` (${caf.reason})` : ''}
      </p>
      <div className="plots">
        <div className="plot">
          <h3>Surface around the peak</h3>
          <Heatmap
            z={caf.surface}
            x={caf.doppler_axis_hz}
            y={caf.delay_axis_s}
            xLabel="Doppler Hz"
            yLabel="delay µs"
            xFormat={(v) => fixed(v, Math.abs((caf.doppler_axis_hz.at(-1) ?? 1) - (caf.doppler_axis_hz[0] ?? 0)) < 2 ? 3 : 1)}
            yFormat={(v) => {
              const span = Math.abs((caf.delay_axis_s.at(-1) ?? 0) - (caf.delay_axis_s[0] ?? 0)) * 1e6;
              return fixed(v * 1e6, span > 100 ? 0 : span > 10 ? 1 : 2);
            }}
            zFormat={(v) => `${fixed(v, 1)} dB`}
            peak={caf.peak_tdoa_s !== null && caf.peak_fdoa_hz !== null ? { x: caf.peak_fdoa_hz, y: caf.peak_tdoa_s } : null}
            others={candidates.length > 1 ? candidates.filter((c) => c.tdoa_s !== null).map((c) => ({ x: c.fdoa_hz, y: c.tdoa_s ?? 0 })) : []}
            ariaLabel="Correlation surface, delay against Doppler, dB above the noise floor"
          />
        </div>
        <div className="plot">
          <h3>Delay cut</h3>
          <LinePlot
            series={[{ x: caf.delay_cut.x.map((v) => v * 1e6), y: caf.delay_cut.db, token: '--ink', label: 'delay cut' }]}
            xLabel="delay µs (whole window)"
            yLabel="dB"
            xFormat={(v) => fixed(v, 0)}
            yFormat={(v) => fixed(v, delayDigits)}
            marker={caf.peak_tdoa_s !== null ? caf.peak_tdoa_s * 1e6 : null}
            readout={(x) => `delay ${fixed(x, 2)} µs`}
            ariaLabel="Delay cut through the peak"
          />
        </div>
        <div className="plot">
          <h3>Doppler cut</h3>
          <LinePlot
            series={[{ x: caf.doppler_cut.x, y: caf.doppler_cut.db, token: '--ink', label: 'Doppler cut' }]}
            xLabel="Doppler Hz (whole window)"
            yLabel="dB"
            xFormat={(v) => fixed(v, Math.abs((caf.doppler_cut.x.at(-1) ?? 0) - (caf.doppler_cut.x[0] ?? 0)) > 20 ? 0 : 1)}
            yFormat={(v) => fixed(v, dopplerDigits)}
            marker={caf.peak_fdoa_hz}
            readout={(x) => `Doppler ${fixed(x, 3)} Hz`}
            ariaLabel="Doppler cut through the peak"
          />
        </div>
      </div>
      {candidates.length > 1 ? (
        <div style={{ marginTop: 10 }}>
          <h3>
            {candidates.length} peaks found{caf.reason === 'ambiguous' ? ', too close in height to choose one' : ''}
          </h3>
          <p className="ink2">
            {caf.reason === 'ambiguous'
              ? 'No time or frequency difference is taken from this correlation. Squares mark the peaks that fall inside the patch above; the strongest is circled when it was measured.'
              : 'The strongest peak is the measurement; the others are listed for reference (squares on the surface).'}
          </p>
          <table className="grid compact" style={{ maxWidth: 520 }}>
            <thead>
              <tr>
                <th className="num">#</th>
                <th className="num">
                  Delay <span className="unit">µs</span>
                </th>
                <th className="num">
                  Doppler <span className="unit">Hz</span>
                </th>
                <th className="num">
                  SNR <span className="unit">dB</span>
                </th>
                <th className="num">
                  Below the first <span className="unit">dB</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {candidates.map((c, i) => (
                <tr key={i}>
                  <td className="num">{i + 1}</td>
                  <td className="num">{c.tdoa_s === null ? 'none (ridge)' : micros(c.tdoa_s)}</td>
                  <td className="num">{hz3(c.fdoa_hz)}</td>
                  <td className="num">{db(c.snr_db)}</td>
                  <td className="num">{db((candidates[0]?.snr_db ?? c.snr_db) - c.snr_db)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      <span className="sr-only">Delay axis in microseconds: {us(caf.delay_axis_s[0] ?? 0)} to {us(caf.delay_axis_s.at(-1) ?? 0)}</span>
    </>
  );
}

function History({ ev }: { ev: Event }) {
  const h = ev.solution_history;
  if (!h.length) return <p className="ink2">No solution yet.</p>;
  const t0 = h[0]?.t_ns ?? '0';
  const x = h.map((r) => nsDiffSeconds(r.t_ns, t0));
  // K18: solves whose measurements were set aside (a break, another transmitter) are drawn
  // apart, in gray, and a dashed rule marks where the current transmission's solves begin
  const split = splitHistory(h);
  const pick = (rows: typeof h) => ({ x: rows.map((r) => nsDiffSeconds(r.t_ns, t0)), major: rows.map((r) => r.semi_major_m / 1000), minor: rows.map((r) => r.semi_minor_m / 1000) });
  const old = pick(split.setAside);
  const cur = pick(split.current);
  const series = [
    ...(old.x.length
      ? [
          { x: old.x, y: old.major, token: '--ink-3', label: 'semi-major, set aside', points: true },
          { x: old.x, y: old.minor, token: '--ink-3', dash: [5, 4], label: 'semi-minor, set aside', points: true },
        ]
      : []),
    { x: cur.x, y: cur.major, token: '--fix', label: 'semi-major', points: true },
    { x: cur.x, y: cur.minor, token: '--fix', dash: [5, 4], label: 'semi-minor', points: true },
  ];
  const note = (r: (typeof h)[number]) =>
    [r.set_aside ? 'set aside' : '', r.fit_consistent === false ? 'inconsistent, enlarged' : r.is_fix === false ? 'not a fix' : ''].filter(Boolean).join('; ');
  return (
    <>
      {split.breakNs ? (
        <p className="ink2" style={{ marginBottom: 6 }}>
          {plural(split.setAside.length, 'earlier solve')} set aside (gray); the current transmission's solves start at <span className="mono">{zuluTime(split.breakNs)}Z</span> (dashed rule).
        </p>
      ) : null}
      <div className="plots" style={{ gridTemplateColumns: '2fr 1fr', alignItems: 'start' }}>
        <div className="plot">
          <LinePlot
            series={series}
            logY
            xLabel={`seconds after ${zuluTime(t0)}Z`}
            yLabel="km (95 % semi-axes)"
            xFormat={(v) => fixed(v, 0)}
            yFormat={(v) => (v >= 1 ? fixed(v, 0) : fixed(v, 1))}
            marker={split.breakNs ? nsDiffSeconds(split.breakNs, t0) : null}
            readout={(xv) => {
              let best = 0;
              x.forEach((v, i) => (Math.abs(v - xv) < Math.abs((x[best] ?? 0) - xv) ? (best = i) : null));
              const r = h[best];
              const n = r ? note(r) : '';
              return r ? `${zuluTime(r.t_ns)}Z  ${r.n_measurements} meas.  semi-axes ${semiAxes(r.semi_major_m, r.semi_minor_m)}${n ? `  (${n})` : ''}` : '';
            }}
            ariaLabel="95 % ellipse semi-axes against time, logarithmic"
          />
        </div>
        <table className="grid">
          <thead>
            <tr>
              <th>Time (Z)</th>
              <th className="num">Meas.</th>
              <th className="num">
                Semi-major <span className="unit">km</span>
              </th>
              <th className="num">
                Semi-minor <span className="unit">km</span>
              </th>
              {h.some((r) => note(r)) ? <th>Note</th> : null}
            </tr>
          </thead>
          <tbody>
            {h.map((r) => (
              <tr key={r.t_ns} className={r.set_aside ? 'muted' : undefined}>
                <td className="mono">{zuluTime(r.t_ns)}</td>
                <td className="num">{r.n_measurements}</td>
                <td className="num">{km(r.semi_major_m)}</td>
                <td className="num">{km(r.semi_minor_m)}</td>
                {h.some((x2) => note(x2)) ? <td>{note(r)}</td> : null}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

// The plots redraw their canvases on every render; an event update (several a round)
// re-renders the dossier, so these re-render only when what they show changed.
const CorrelationView = memo(Correlation, (a, b) => a.ev.id === b.ev.id && a.ev.caf?.t_ns === b.ev.caf?.t_ns);
const historyKey = (ev: Event) =>
  ev.solution_history.map((r) => `${r.t_ns}${r.set_aside ? 's' : ''}${r.fit_consistent === false ? 'i' : ''}${r.is_fix === false ? 'n' : ''}`).join('|');
const HistoryView = memo(History, (a, b) => a.ev.id === b.ev.id && historyKey(a.ev) === historyKey(b.ev));

const TASKING_WORD: Record<TaskingStatus['state'], string> = {
  waiting: 'waiting for the next round',
  capturing: 'capturing IQ now',
  processing: 'correlating and solving now',
  idle: 'idle',
};

/** What capture tasking is doing for the event (Event.tasking). */
function TaskingLine({ t, active, live }: { t: TaskingStatus; active: boolean; live: boolean }) {
  useTick(1000);
  const nowMs = serverNowMs();
  // UX-8: what tasking is doing now is unknown while the console is disconnected
  let phrase = !live && active ? 'unknown (console disconnected)' : active ? TASKING_WORD[t.state] : 'stopped';
  if (live && active && t.state === 'waiting' && t.next_round_ns) {
    const inS = (nsToMs(t.next_round_ns) - nowMs) / 1000;
    phrase = inS > 0.5 ? `next round in ${formatAge(inS)}` : 'next round due now';
  }
  return (
    <span className="tasking">
      Captures <b>{phrase}</b> ({plural(t.rounds, 'round')} so far
      {t.channels.length ? (
        <>
          , last on <span className="mono">{t.channels.join(', ')}</span>
        </>
      ) : null}
      )
      {t.message ? <span className="tmsg">{t.message}</span> : null}
    </span>
  );
}

function CloseButton({ ev }: { ev: Event }) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (!isActive(ev)) return null;
  if (!confirming)
    return (
      <button type="button" className="btn" onClick={() => setConfirming(true)}>
        Close event
      </button>
    );
  return (
    <span style={{ display: 'inline-flex', gap: 8, alignItems: 'center' }}>
      <span>Close {ev.id}? It leaves the active list and cannot be reopened.</span>
      <button
        type="button"
        className="btn primary"
        disabled={busy}
        onClick={async () => {
          setBusy(true);
          try {
            await api.closeEvent(ev.id);
            await loadEvent(ev.id);
            setConfirming(false);
          } catch (e) {
            setError((e as Error).message);
          } finally {
            setBusy(false);
          }
        }}
      >
        Close {ev.id}
      </button>
      <button type="button" className="btn" onClick={() => setConfirming(false)}>
        Keep open
      </button>
      {error ? <span className="err-text">{error}</span> : null}
    </span>
  );
}

function exportEvent(ev: Event): void {
  const blob = new Blob([JSON.stringify({ exported_at: new Date().toISOString(), first_seen: nsToIso(ev.first_seen_ns), last_seen: nsToIso(ev.last_seen_ns), event: ev }, null, 2)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `${ev.id}-${nsToIso(ev.last_seen_ns, 0).replace(/:/g, '')}.json`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

/** ADAPT-5: why a width is a lower bound (Event.width_lower_bound). */
function WidthInfo() {
  return (
    <>
      {' '}
      <Info label="width lower bound">Every detection of this signal reaches both edges of the monitor window it was measured in, so the signal may be wider than the range shown.</Info>
    </>
  );
}

/** Header of the dossier: what the event is, where it is received, when it began and what tasking does. */
function DossierHead({ ev, cfg }: { ev: Event; cfg: SystemConfig | null }) {
  useTick(1000);
  const link = useLive((s) => s.link);
  const status = useLive((s) => s.status);
  const { live, asOfMs } = freshness({ link, status }, Date.now());
  const nowMs = serverNowMs();
  const level = eventLevel(ev);
  const lastAge = (nowMs - nsToMs(ev.last_seen_ns)) / 1000;
  const onset = ev.onset_ns ?? ev.first_seen_ns;
  // UX-3: a local signal is described by what is on the air at the site that sees it
  const rx = ev.verdict === 'local' ? received(ev, cfg) : null;
  const hidden = ev.not_visible_satellite_ids ?? [];
  // UX-8: "now" only while live; otherwise the last time the console heard about it
  const lastSeen = isActive(ev) && live && lastAge < 15 ? 'now' : `${dtgNs(ev.last_seen_ns)} (${formatAge(lastAge)} ago)`;
  return (
    <div className="dhead">
      <div className="titleline">
        <h1 className="mono">{ev.id}</h1>
        <Status level={level} word={`${VERDICT_WORD[ev.verdict]}`} />
        <span className="ink2">{STATE_WORD[ev.state]}</span>
        {ev.simulated ? <SimTag /> : null}
        {ev.merged_into ? <span>merged into <a href={href('event', ev.merged_into)}>{ev.merged_into}</a></span> : null}
        {!live && isActive(ev) ? <span className="ink2">as of {asOfMs === null ? 'no update' : zuluClock(new Date(asOfMs + (nowMs - Date.now())))}</span> : null}
        <span style={{ flex: 1 }} />
        <button type="button" className="btn" onClick={() => exportEvent(ev)}>
          Export JSON
        </button>
        <CloseButton ev={ev} />
        <a className="btn" href={href('watch')}>
          Map
        </a>
      </div>
      <div className="facts">
        {rx ? (
          <>
            <span>
              Received <b className="mono">{rx.range}</b>
              {ev.width_lower_bound && ev.received_lo_hz !== null && ev.received_lo_hz !== undefined && ev.received_hi_hz !== null && ev.received_hi_hz !== undefined ? (
                <>
                  {' '}
                  ({widthText(ev.received_lo_hz, ev.received_hi_hz, true)})
                  <WidthInfo />
                </>
              ) : null}{' '}
              at <b>{rx.sites.join(', ') || '—'}</b>
            </span>
            <span>
              On{' '}
              {rx.channels.map((c, i) => (
                <span key={c.channel}>
                  {i ? ', ' : ''}
                  <b className="mono">{c.channel}</b>
                  {c.sat ? <span className="mono"> ({c.sat})</span> : null}
                </span>
              ))}
            </span>
          </>
        ) : (
          <>
            <span>
              Uplink{' '}
              <b className="mono">
                {ev.uplink_lo_hz !== null && ev.uplink_hi_hz !== null ? `${mhz(ev.uplink_lo_hz)} – ${mhz(ev.uplink_hi_hz)} MHz` : 'outside every transponder'}
              </b>
              {ev.uplink_lo_hz !== null && ev.uplink_hi_hz !== null ? ` (${widthText(ev.uplink_lo_hz, ev.uplink_hi_hz, ev.width_lower_bound)})` : ''}
              {ev.width_lower_bound ? <WidthInfo /> : null}
            </span>
            <span>
              Downlink <span className="unit">MHz</span>{' '}
              {ev.transponders.length ? (
                ev.transponders.map((t, i) => (
                  <span key={`${t.sat_id}/${t.transponder_id}`} className="nowrap">
                    {i ? ';  ' : ''}
                    <b className="mono">
                      {t.sat_id} {t.transponder_id}
                    </b>{' '}
                    <span className="mono">
                      {mhz(t.downlink_lo_hz, 4)}–{mhz(t.downlink_hi_hz, 4)}
                    </span>
                  </span>
                ))
              ) : ev.downlink_lo_hz !== null && ev.downlink_lo_hz !== undefined ? (
                <b className="mono">
                  {mhz(ev.downlink_lo_hz, 4)}–{mhz(ev.downlink_hi_hz ?? ev.downlink_lo_hz, 4)}
                </b>
              ) : (
                '—'
              )}
            </span>
          </>
        )}
        {hidden.length ? (
          // ADAPT-1: satellites of the plan that cannot see the estimated position
          <span>
            Not affected <b className="mono">{hidden.join(', ')}</b> (cannot see the estimated position)
          </span>
        ) : null}
        {/* UX-5: the start of the current transmission is the time to report */}
        <span>
          Start <b className="mono">{dtgSecondsNs(onset)}</b>
        </span>
        {onset !== ev.first_seen_ns ? (
          <span>
            First seen <b className="mono">{dtgNs(ev.first_seen_ns)}</b>
          </span>
        ) : null}
        {ev.resumed_ns ? (
          <span>
            Detected again <b className="mono">{dtgSecondsNs(ev.resumed_ns)}</b>
            {ev.break_s !== null && ev.break_s !== undefined ? ` after a break of ${formatAge(ev.break_s)}` : ''}
          </span>
        ) : null}
        <span>
          Last seen <b className="mono">{lastSeen}</b>
        </span>
        <span>
          Class <b>{ev.classification ? `${ev.classification.label}, ${percent(ev.classification.confidence)} confidence` : 'not classified yet'}</b>
        </span>
        {ev.tasking ? <TaskingLine t={ev.tasking} active={isActive(ev)} live={live} /> : null}
      </div>
    </div>
  );
}

export function EventDossier({ id }: { id: string }) {
  const cached = useLive((s) => s.events.get(id) ?? null);
  const [error, setError] = useState<string | null>(null);
  const cfg = configResource.use().data?.envelope.config ?? null;
  useEffect(() => {
    let live = true;
    loadEvent(id).catch((e: Error) => live && setError(e.message));
    return () => {
      live = false;
    };
  }, [id]);
  if (!cached) {
    if (error) {
      return (
        <div className="dossier">
          <ErrorNote error={`Event ${id} could not be read: ${error}`} retry={() => navigate(href('event', id), true)} />
          <p style={{ padding: '0 14px' }}>
            <a href={href('watch')}>Back to the map</a>
          </p>
        </div>
      );
    }
    return (
      <div className="dossier">
        <Loading what={`event ${id}`} />
      </div>
    );
  }
  const ev = cached;
  return (
    <article className="dossier" aria-label={`Event ${ev.id}`}>
      <DossierHead ev={ev} cfg={cfg} />

      <section className="advisory" aria-label="Advisory">
        <h2>{ev.advisory.headline}</h2>
        <p>{ev.advisory.detail}</p>
        {ev.advisory.actions.length ? (
          <ul>
            {ev.advisory.actions.map((a) => (
              <li key={a}>{a}</li>
            ))}
          </ul>
        ) : null}
      </section>

      <section className="dsec" aria-label="Verdict and evidence">
        <h2>
          Verdict: {VERDICT_WORD[ev.verdict]}
          <Info label="the evidence matrix">
            Rows are sites, columns satellites. A satellite column seen at sites far apart means the signal is on the uplink; a single site seeing it while others that
            could see it do not means it is local to that site (MATH.md section 7).
          </Info>
          {ev.verdict_reason ? <span className="aside">{ev.verdict_reason}</span> : null}
        </h2>
        <EvidenceMatrix ev={ev} cfg={cfg} />
      </section>

      <div className="dgrid">
        <section aria-label="Location">
          <Location ev={ev} />
        </section>
        <section aria-label="Event map" style={{ padding: 8 }}>
          <EventMap ev={ev} cfg={cfg} />
        </section>
      </div>

      <section className="dsec" aria-label="Measurements">
        <h2>Measurements</h2>
        {ev.measurements.length ? <MeasurementTable rows={ev.measurements} /> : <p className="ink2">No TDOA or FDOA measurements.</p>}
      </section>

      <section className="dsec" aria-label="Correlation">
        <h2>Correlation</h2>
        <CorrelationView ev={ev} />
      </section>

      <section className="dsec" aria-label="Ellipse history">
        <h2>Ellipse against time</h2>
        <HistoryView ev={ev} />
      </section>

      <section className="dsec" aria-label="Transitions">
        <h2>Transitions</h2>
        <table className="grid" style={{ maxWidth: 760 }}>
          <thead>
            <tr>
              <th>Time (Z)</th>
              <th>State</th>
              <th>Verdict</th>
              <th>Why</th>
            </tr>
          </thead>
          <tbody>
            {ev.transitions.map((t, i) => (
              <tr key={`${t.t_ns}-${i}`}>
                <td className="mono">{dtgSecondsNs(t.t_ns)}</td>
                <td>{STATE_WORD[t.state]}</td>
                <td>{VERDICT_WORD[t.verdict]}</td>
                <td className="wrap">{t.reason ?? ''}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
      <span className="sr-only">
        Fix {ev.solution ? `${formatLat(ev.solution.lat_deg)} ${formatLon(ev.solution.lon_deg)}` : 'none'}
      </span>
    </article>
  );
}
