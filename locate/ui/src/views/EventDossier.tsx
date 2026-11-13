// Event dossier: what the operator must decide first (verdict and advisory), the evidence
// for it (the evidence matrix), where the emitter is and how sure the system is (fix,
// ellipse, ambiguity, lines of position, measurements, correlation surface, history).
import { useEffect, useMemo, useState } from 'react';
import type { Event, EvidenceRow, LopCollection, MeasurementRow, NodeStatus, SolutionOut, SystemConfig, TaskingStatus } from '../api/types.gen';
import { ErrorNote, Info, Loading, SimTag, Status, useTick } from '../components/common';
import { Heatmap, LinePlot } from '../components/plots';
import { eventLevel, isActive, STATE_WORD, VERDICT_WORD } from '../domain/status';
import { DATUM, distanceM, formatLat, formatLatLon, formatLon, formatMgrs } from '../format/geo';
import { db, deg, fixed, hz3, km, mhz, micros, millihertz, percent, plural } from '../format/units';
import { DOMINANT_WORD, MapView, type MapData, type MapFocus, type MapSolution } from '../map/MapView';
import { href, navigate } from '../router';
import { configResource, subpointsResource } from '../state/resources';
import { api, loadEvent, serverNowMs, useLive } from '../state/store';
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

function evidenceNumbers(r: EvidenceRow, nowMs: number, node: NodeStatus | undefined): string {
  switch (r.state) {
    case 'seen':
      return `SNR ${db(r.snr_db)} dB, C/N0 ${db(r.cn0_dbhz)} dBHz`;
    case 'not_seen_capable':
      return r.predicted_margin_db !== null ? `expected +${db(r.predicted_margin_db)} dB margin` : 'expected to see it';
    case 'not_capable':
      return r.predicted_margin_db !== null ? `margin ${db(r.predicted_margin_db)} dB` : 'no path';
    case 'stale': {
      // how old the evidence is: the channel's newest spectrum frame (status), and when it last saw the signal
      const parts: string[] = [];
      const age = node?.channels.find((c) => c.channel_id === r.channel_id)?.last_frame_age_s;
      if (node && !node.online) parts.push('node offline');
      if (age !== null && age !== undefined) parts.push(`no frame for ${formatAge(age)}`);
      else if (!node || node.online) parts.push('no recent frames');
      if (r.last_seen_ns) parts.push(`detected ${formatAge((nowMs - nsToMs(r.last_seen_ns)) / 1000)} ago`);
      return parts.join(', ');
    }
  }
}

/** Signature element: sites x satellites, one cell per receive channel. */
function EvidenceMatrix({ ev, cfg }: { ev: Event; cfg: SystemConfig | null }) {
  const nowMs = serverNowMs();
  const nodes = useLive((s) => s.status?.nodes ?? null);
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
                          <span className="n">{evidenceNumbers(r, nowMs, nodeOf(r.site_id))}</span>
                        </div>
                      ))}
                    </td>
                  );
                })}
                {noSat.length ? (
                  <td>
                    {noSat
                      .filter((r) => r.site_id === site)
                      .map((r) => (
                        <div className="ev" key={r.channel_id}>
                          <EvGlyph state={r.state} />
                          <span className="w">{EV_WORD[r.state]}</span>
                          <span className="n">{evidenceNumbers(r, nowMs, nodeOf(r.site_id))}</span>
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

const REASON_WORD: Record<string, string> = {
  clock_unlocked: 'clock unlocked',
  ambiguous: 'ambiguous peak',
  narrowband: 'too narrow to time',
  not_detected: 'not detected',
  'below threshold': 'below threshold',
  'delay-doppler coupled': 'delay and Doppler coupled',
  'peak outside window': 'peak outside window',
};

function MeasurementTable({ rows }: { rows: MeasurementRow[] }) {
  const [usedOnly, setUsedOnly] = useState(false);
  const sorted = useMemo(() => [...rows].sort((a, b) => (BigInt(b.t_ns) > BigInt(a.t_ns) ? 1 : BigInt(b.t_ns) < BigInt(a.t_ns) ? -1 : a.kind.localeCompare(b.kind))), [rows]);
  const shown = usedOnly ? sorted.filter((r) => r.used) : sorted;
  const used = rows.filter((r) => r.used).length;
  return (
    <>
      <div style={{ display: 'flex', gap: 14, alignItems: 'center', marginBottom: 6 }}>
        <span className="ink2">
          {used} of {rows.length} used by the solver
        </span>
        <label>
          <input type="checkbox" checked={usedOnly} onChange={(e) => setUsedOnly(e.target.checked)} /> Used only
        </label>
      </div>
      <div className="tablewrap" style={{ maxHeight: 340 }}>
        <table className="grid compact">
          <thead>
            <tr>
              <th>Time (Z)</th>
              <th>Pair (A − B)</th>
              <th className="num">
                TDOA <span className="unit">µs</span>
              </th>
              <th className="num">
                <span className="unit">σ µs</span>
              </th>
              <th className="num">
                FDOA <span className="unit">Hz</span>
              </th>
              <th className="num">
                <span className="unit">σ mHz</span>
              </th>
              <th className="num" title="Correlation (post-integration) SNR">
                SNR <span className="unit">dB</span>
              </th>
              <th className="num" title="Whitened residual against the current solution">
                Resid. <span className="unit">σ</span>
              </th>
              <th title="Reference-emitter calibration applied">Cal.</th>
              <th>Used</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((r, i) => (
              <tr key={`${r.t_ns}-${r.kind}-${pairText(r)}-${i}`} className={r.used ? undefined : 'muted'}>
                <td className="mono">{zuluTime(r.t_ns)}</td>
                <td className="mono">{pairText(r)}</td>
                <td className="num">{r.kind === 'tdoa' ? micros(r.value) : ''}</td>
                <td className="num">{r.kind === 'tdoa' ? micros(r.sigma) : ''}</td>
                <td className="num">{r.kind === 'fdoa' ? hz3(r.value) : ''}</td>
                <td className="num">{r.kind === 'fdoa' ? millihertz(r.sigma) : ''}</td>
                <td className="num">{db(r.snr_db)}</td>
                <td className="num">{fixed(r.residual, 2)}</td>
                <td>{r.calibrated ? 'yes' : 'no'}</td>
                <td className="wrap">{r.used ? 'yes' : `no: ${REASON_WORD[r.reason ?? ''] ?? r.reason ?? 'not used'}`}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

const RESOLVED_TEXT: Record<SolutionOut['ambiguity_resolved_by'], string> = {
  none: 'No second position fits the measurements.',
  region: 'A second position (the mirror) fits as well, but lies outside the region of interest or below the elevation mask, so it was ruled out.',
  measurements: 'A second position was found but fits the measurements clearly worse.',
  unresolved: 'Another position fits the measurements as well as this one. Do not act on this position alone.',
};

function shareOf(sol: SolutionOut): { noise: number; consider: number } {
  const tr = (m: number[][]) => (m[0]?.[0] ?? 0) + (m[1]?.[1] ?? 0);
  const total = tr(sol.cov_en_m2) || 1;
  return { noise: tr(sol.cov_noise_en_m2) / total, consider: tr(sol.cov_consider_en_m2) / total };
}

function Location({ ev }: { ev: Event }) {
  const sol = ev.solution;
  if (!sol) {
    const t = ev.tasking;
    const why =
      ev.verdict === 'local'
        ? 'Local interference enters at one site, not through the satellites, so it is not located by time and frequency differences.'
        : t?.solve_reason
          ? `The last solve gave no position: ${t.solve_reason}.`
          : t?.state === 'idle' && t.message
            ? t.message
            : ev.satellite_ids.length < 2
              ? 'Only one satellite relays this range, so there is no satellite pair to correlate.'
              : ev.measurements.length === 0
                ? 'No correlation result yet.'
                : `Not enough usable measurements yet (${ev.measurements.filter((m) => m.used).length} usable; two are needed).`;
    return (
      <div>
        <h2>No position</h2>
        <p className="ink2">{why}</p>
      </div>
    );
  }
  const e = sol.ellipse;
  const mgrs = formatMgrs(sol.lat_deg, sol.lon_deg);
  const share = shareOf(sol);
  const truth = ev.truth;
  return (
    <div>
      <div className="fixline mono">
        {formatLatLon(sol.lat_deg, sol.lon_deg, 4)} <span className="ink2" style={{ fontSize: 12 }}>{DATUM}</span>
      </div>
      <div className="mono" style={{ marginBottom: 8 }}>
        MGRS {mgrs ?? 'not defined at this latitude (UPS zone)'}
      </div>
      <dl className="facts">
        <dt>95 % ellipse</dt>
        <dd>
          <span className="mono">
            {km(e.semi_major_m)} {'×'} {km(e.semi_minor_m)} km
          </span>{' '}
          semi-axes, major axis {deg(e.azimuth_deg, 0)}{'°'} true
        </dd>
        <dt>CEP50</dt>
        <dd className="mono">{km(sol.cep50_m)} km</dd>
        <dt>
          Dominant error{' '}
          <Info label="dominant error">
            The largest contributor to the position variance. Systematic terms (ephemeris, clocks, oscillators, TDOA bias, reference) do not shrink with more
            captures; noise does. A reference emitter on the same pair removes most of the ephemeris term.
          </Info>
        </dt>
        <dd>
          {DOMINANT_WORD[sol.dominant_error] ?? sol.dominant_error}: noise {percent(share.noise)}, systematic {percent(share.consider)} of the variance
        </dd>
        <dt>Fit</dt>
        <dd>
          {sol.n_measurements} measurements, {'χ²'} {fixed(sol.chi2, 1)} on {sol.dof} degrees of freedom
          {sol.reduced_chi2 !== null ? ` (reduced ${fixed(sol.reduced_chi2, 2)})` : ''}
        </dd>
        <dt>Solved</dt>
        <dd className="mono">{ev.solution_t_ns ? dtgSecondsNs(ev.solution_t_ns) : '—'}</dd>
        <dt>Ambiguity</dt>
        <dd>
          {RESOLVED_TEXT[sol.ambiguity_resolved_by]}
          {sol.ambiguities.map((a, i) => (
            <div key={i} className="mono" style={{ marginTop: 2 }}>
              Mirror {i + 1}: {formatLatLon(a.lat_deg, a.lon_deg, 3)}, {km(distanceM(sol.lat_deg, sol.lon_deg, a.lat_deg, a.lon_deg))} km away, {'Δχ²'} {fixed(a.chi2 - sol.chi2, 2)}
              , 95 % {km(a.ellipse.semi_major_m)} {'×'} {km(a.ellipse.semi_minor_m)} km (dashed on the map)
            </div>
          ))}
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
  const data: MapData = useMemo(() => {
    const solutions: MapSolution[] = [];
    if (sol) {
      solutions.push({ id: 'fix', eventId: ev.id, lat: sol.lat_deg, lon: sol.lon_deg, semiMajorM: sol.ellipse.semi_major_m, semiMinorM: sol.ellipse.semi_minor_m, azimuthDeg: sol.ellipse.azimuth_deg, role: 'fix', selected: true, label: ev.id });
      sol.ambiguities.forEach((a, i) =>
        solutions.push({ id: `mirror-${i}`, eventId: ev.id, lat: a.lat_deg, lon: a.lon_deg, semiMajorM: a.ellipse.semi_major_m, semiMinorM: a.ellipse.semi_minor_m, azimuthDeg: a.ellipse.azimuth_deg, role: 'mirror', selected: false, label: `mirror ${i + 1}` }),
      );
    }
    return {
      sites: cfg?.sites.map((s) => ({ id: s.id, name: s.name, lat: s.lat_deg, lon: s.lon_deg })) ?? [],
      subpoints: subs.data ?? [],
      solutions,
      lops: lop,
      truth: ev.truth ? [{ id: ev.truth.emitter_id, lat: ev.truth.lat_deg, lon: ev.truth.lon_deg, label: 'truth (SIMULATION)' }] : [],
    };
  }, [ev, cfg, subs.data, lop, sol]);
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

function Correlation({ ev }: { ev: Event }) {
  const caf = ev.caf;
  if (!caf) return <p className="ink2">No correlation yet.</p>;
  const us = (s: number) => `${fixed(s * 1e6, 1)}`;
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
            yFormat={(v) => fixed(v, 0)}
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
            xFormat={(v) => fixed(v, 1)}
            yFormat={(v) => fixed(v, 0)}
            marker={caf.peak_fdoa_hz}
            readout={(x) => `Doppler ${fixed(x, 3)} Hz`}
            ariaLabel="Doppler cut through the peak"
          />
        </div>
      </div>
      <span className="sr-only">Delay axis in microseconds: {us(caf.delay_axis_s[0] ?? 0)} to {us(caf.delay_axis_s.at(-1) ?? 0)}</span>
    </>
  );
}

function History({ ev }: { ev: Event }) {
  const h = ev.solution_history;
  if (!h.length) return <p className="ink2">No solution yet.</p>;
  const t0 = h[0]?.t_ns ?? '0';
  const x = h.map((r) => nsDiffSeconds(r.t_ns, t0));
  return (
    <div className="plots" style={{ gridTemplateColumns: '2fr 1fr' }}>
      <div className="plot">
        <LinePlot
          series={[
            { x, y: h.map((r) => r.semi_major_m / 1000), token: '--fix', label: 'semi-major', points: true },
            { x, y: h.map((r) => r.semi_minor_m / 1000), token: '--fix', dash: [5, 4], label: 'semi-minor', points: true },
          ]}
          logY
          xLabel={`seconds after ${zuluTime(t0)}Z`}
          yLabel="km (95 %)"
          xFormat={(v) => fixed(v, 0)}
          yFormat={(v) => (v >= 1 ? fixed(v, 0) : fixed(v, 1))}
          readout={(xv) => {
            let best = 0;
            x.forEach((v, i) => (Math.abs(v - xv) < Math.abs((x[best] ?? 0) - xv) ? (best = i) : null));
            const r = h[best];
            return r ? `${zuluTime(r.t_ns)}Z  ${r.n_measurements} meas.  ${km(r.semi_major_m)} × ${km(r.semi_minor_m)} km` : '';
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
              Major <span className="unit">km</span>
            </th>
            <th className="num">
              Minor <span className="unit">km</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {h.map((r) => (
            <tr key={r.t_ns}>
              <td className="mono">{zuluTime(r.t_ns)}</td>
              <td className="num">{r.n_measurements}</td>
              <td className="num">{km(r.semi_major_m)}</td>
              <td className="num">{km(r.semi_minor_m)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const TASKING_WORD: Record<TaskingStatus['state'], string> = {
  waiting: 'waiting for the next round',
  capturing: 'capturing IQ',
  processing: 'correlating and solving',
  idle: 'idle',
};

/** What capture tasking is doing for the event (Event.tasking). */
function TaskingLine({ t, active }: { t: TaskingStatus; active: boolean }) {
  useTick(1000);
  const nowMs = serverNowMs();
  let next = '';
  if (active && t.state === 'waiting' && t.next_round_ns) {
    const inS = (nsToMs(t.next_round_ns) - nowMs) / 1000;
    next = inS > 0.5 ? `, next in ${formatAge(inS)}` : ', next due now';
  }
  return (
    <span className="tasking">
      Captures{' '}
      <b>
        {active ? TASKING_WORD[t.state] : 'stopped'}, {plural(t.rounds, 'round')}
        {next}
      </b>
      {t.channels.length ? (
        <>
          {' '}
          on <span className="mono">{t.channels.join(', ')}</span>
        </>
      ) : null}
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
  const level = eventLevel(ev);
  const first = dtgNs(ev.first_seen_ns);
  const lastAge = (serverNowMs() - nsToMs(ev.last_seen_ns)) / 1000;
  return (
    <article className="dossier" aria-label={`Event ${ev.id}`}>
      <div className="dhead">
        <div className="titleline">
          <h1 className="mono">{ev.id}</h1>
          <Status level={level} word={`${VERDICT_WORD[ev.verdict]}`} />
          <span className="ink2">{STATE_WORD[ev.state]}</span>
          {ev.simulated ? <SimTag /> : null}
          {ev.merged_into ? <span>merged into <a href={href('event', ev.merged_into)}>{ev.merged_into}</a></span> : null}
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
          <span>
            Uplink{' '}
            <b className="mono">
              {ev.uplink_lo_hz !== null && ev.uplink_hi_hz !== null ? `${mhz(ev.uplink_lo_hz)} – ${mhz(ev.uplink_hi_hz)} MHz` : 'outside every transponder'}
            </b>
            {ev.uplink_lo_hz !== null && ev.uplink_hi_hz !== null ? ` (${fixed((ev.uplink_hi_hz - ev.uplink_lo_hz) / 1000, 2)} kHz)` : ''}
          </span>
          <span>
            Downlink{' '}
            {ev.transponders.length ? (
              ev.transponders.map((t, i) => (
                <span key={`${t.sat_id}/${t.transponder_id}`}>
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
            )}{' '}
            MHz
          </span>
          <span>
            First seen <b className="mono">{first}</b>
          </span>
          <span>
            Last seen <b className="mono">{isActive(ev) && lastAge < 15 ? 'now' : `${dtgNs(ev.last_seen_ns)} (${formatAge(lastAge)} ago)`}</b>
          </span>
          <span>
            Class <b>{ev.classification ? `${ev.classification.label}, ${percent(ev.classification.confidence)} confidence` : 'not classified yet'}</b>
          </span>
          {ev.tasking ? <TaskingLine t={ev.tasking} active={isActive(ev)} /> : null}
        </div>
      </div>

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
        <Correlation ev={ev} />
      </section>

      <section className="dsec" aria-label="Ellipse history">
        <h2>Ellipse against time</h2>
        <History ev={ev} />
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
