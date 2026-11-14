// Planning: who sees which satellite (look angles, footprints) and how well an emitter
// anywhere in the region would be located (accuracy map, MATH.md section 10).
import { useEffect, useMemo, useState } from 'react';
import { ValidationError } from '../api/client';
import type { AccuracyGrid, AccuracyRequest, LookAngle, SystemConfig } from '../api/types.gen';
import { Empty, ErrorNote, Info, Loading, StatusSymbol } from '../components/common';
import { transpondersRelaying } from '../domain/config';
import { accuracyDefaults, NEAREST_REFERENCE, pairEnds, type PairEnd } from '../domain/plan';
import { db, deg, fixed, hzToMhzInput, km, mhz, parseMhzToHz } from '../format/units';
import { DOMINANT_KEY, DOMINANT_WORD, MapView, type MapData } from '../map/MapView';
import { href, setParams, useRoute } from '../router';
import { configResource, footprintsResource, lookAnglesResource, subpointsResource } from '../state/resources';
import { api } from '../state/store';
import { useLayers } from './Watch';

function siteLabel(cfg: SystemConfig, id: string): string {
  return cfg.sites.find((s) => s.id === id)?.name.replace(/\s*\(.*\)$/, '') ?? id;
}

function VisibilityMatrix({ cfg, looks }: { cfg: SystemConfig; looks: LookAngle[] }) {
  const bySiteSat = new Map<string, LookAngle>();
  for (const l of looks) if (!bySiteSat.has(`${l.site_id}|${l.sat_id}`)) bySiteSat.set(`${l.site_id}|${l.sat_id}`, l);
  return (
    <table className="grid" aria-label="Elevation of each satellite from each site">
      <thead>
        <tr>
          <th>Site</th>
          {cfg.satellites.map((s) => (
            <th key={s.id} className="num mono">
              {s.id} <span className="unit">el °</span>
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {cfg.sites.map((site) => (
          <tr key={site.id}>
            <td>{siteLabel(cfg, site.id)}</td>
            {cfg.satellites.map((sat) => {
              const l = bySiteSat.get(`${site.id}|${sat.id}`);
              if (!l) return <td key={sat.id} className="num muted">—</td>;
              return l.visible ? (
                <td key={sat.id} className="num" title={`azimuth ${deg(l.az_deg)}°, range ${fixed(l.range_km, 0)} km`}>
                  {deg(l.el_deg)}
                </td>
              ) : (
                <td key={sat.id} className="num hatch muted">
                  below {deg(l.el_deg)}
                </td>
              );
            })}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function LookTable({ cfg, looks, watchedOnly }: { cfg: SystemConfig; looks: LookAngle[]; watchedOnly: boolean }) {
  const rows = looks.filter((l) => !watchedOnly || l.watched);
  const antennaText = (l: LookAngle) => {
    const ch = cfg.sites.flatMap((s) => s.channels).find((c) => c.id === l.channel_id);
    const a = ch?.antenna;
    const kind = !a ? '' : a.type === 'parabolic' ? `${fixed(a.diameter_m, 2)} m dish` : a.type === 'beam' ? `beam ${db(a.gain_dbi)} dBi` : a.type === 'isotropic' ? 'isotropic' : 'pattern table';
    if (!l.electrically_small) return kind;
    // MATH/ARCHITECTURE: a dish under 3 wavelengths across behaves as a reflector, not a focusing dish
    const wl = 299_792_458 / l.freq_hz;
    const across = a && a.type === 'parabolic' ? a.diameter_m / wl : null;
    return `${kind}: reflector, ${across !== null ? `${fixed(across, 2)} λ` : 'under 3 λ'}`;
  };
  return (
    <table className="grid compact" aria-label="Look angles of every channel to every satellite">
      <thead>
        <tr>
          <th>Site</th>
          <th>Channel</th>
          <th>Satellite</th>
          {watchedOnly ? null : <th>Watched</th>}
          <th className="num">
            Az <span className="unit">°</span>
          </th>
          <th className="num">
            El <span className="unit">°</span>
          </th>
          <th className="num">
            Range <span className="unit">km</span>
          </th>
          <th className="num">
            Off-axis <span className="unit">°</span>
          </th>
          <th className="num">
            Gain <span className="unit">dBi</span>
          </th>
          <th>Antenna</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((l) => (
          <tr key={`${l.channel_id}|${l.sat_id}`} className={l.visible ? undefined : 'hatched muted'}>
            <td>{siteLabel(cfg, l.site_id)}</td>
            <td className="mono">{l.channel_id}</td>
            <td className="mono">{l.sat_id}</td>
            {watchedOnly ? null : <td>{l.watched ? 'yes' : 'no'}</td>}
            <td className="num">{deg(l.az_deg)}</td>
            <td className="num">{l.visible ? deg(l.el_deg) : `${deg(l.el_deg)} below`}</td>
            <td className="num">{fixed(l.range_km, 0)}</td>
            <td className="num">{deg(l.off_axis_deg)}</td>
            <td className="num" title={`peak ${db(l.peak_gain_dbi)} dBi at ${mhz(l.freq_hz, 3)} MHz`}>{db(l.gain_dbi)}</td>
            <td>
              {l.electrically_small ? (
                <span className="status" title="Under 3 wavelengths across: it behaves as a reflector with a broad beam, not a focusing dish">
                  <StatusSymbol level="caution" title="Caution" /> {antennaText(l)}
                </span>
              ) : (
                antennaText(l)
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function LookAngles({ cfg }: { cfg: SystemConfig }) {
  const looks = lookAnglesResource.use();
  const subs = subpointsResource.use();
  const route = useRoute();
  const minEl = Number(route.params.get('el') ?? 0);
  const fps = footprintsResource(minEl).use();
  const watchedOnly = route.params.get('all') !== '1';
  const [layers, setLayers] = useLayers('plan-look', { sites: true, satellites: true, footprints: true });
  const data: MapData = useMemo(
    () => ({ sites: cfg.sites.map((s) => ({ id: s.id, name: s.name, lat: s.lat_deg, lon: s.lon_deg })), subpoints: subs.data ?? [], footprints: fps.data ?? [] }),
    [cfg, subs.data, fps.data],
  );
  return (
    <div className="split">
      <div className="side">
        <div className="region">
          <h2>Elevation from each site</h2>
          {looks.error ? <ErrorNote error={looks.error} retry={looks.reload} /> : looks.data ? <VisibilityMatrix cfg={cfg} looks={looks.data} /> : <Loading what="look angles" />}
        </div>
        <div className="region">
          <h2>Footprints</h2>
          <label>
            Edge at elevation{' '}
            <select value={minEl} onChange={(e) => setParams({ el: e.target.value === '0' ? null : e.target.value })}>
              {[0, 5, 10, 20].map((v) => (
                <option key={v} value={v}>
                  {v}°
                </option>
              ))}
            </select>
          </label>
        </div>
      </div>
      <div className="main">
        <div style={{ height: '55%', minHeight: 300, display: 'flex' }}>
          <MapView label="Satellite footprints and sites" data={data} layers={layers} onLayers={setLayers} region={cfg.processing?.region ?? null} />
        </div>
        <div className="toolbar">
          <h2 style={{ margin: 0 }}>Channels</h2>
          <label>
            <input type="checkbox" checked={!watchedOnly} onChange={(e) => setParams({ all: e.target.checked ? '1' : null })} /> Every channel by every satellite
          </label>
        </div>
        <div className="tablewrap" style={{ flex: 1 }}>{looks.data ? <LookTable cfg={cfg} looks={looks.data} watchedOnly={watchedOnly} /> : null}</div>
      </div>
    </div>
  );
}

interface AccForm {
  pairs: Array<[string, string]>;
  eirp: string;
  bwKhz: string;
  waveform: 'noise' | 'cw' | 'chirp' | 'psk';
  feMhz: string;
  tdoa: boolean;
  fdoa: boolean;
  reference: string;
  integration: string;
  step: string;
}

function Accuracy({ cfg }: { cfg: SystemConfig }) {
  // ADAPT-4: a channel watching several satellites offers one "channel@sat" end per satellite
  const ends = useMemo(() => pairEnds(cfg), [cfg]);
  const refs = (cfg.carriers ?? []).filter((c) => c.use_as_reference);
  // ADAPT-10: the starting request comes from the loaded configuration, whatever it holds
  const [form, setForm] = useState<AccForm>(() => {
    const d = accuracyDefaults(cfg);
    return {
      pairs: d.pairs,
      eirp: '25',
      bwKhz: String(Math.round(d.bwHz / 100) / 10),
      waveform: 'noise',
      feMhz: d.feHz !== null ? hzToMhzInput(d.feHz) : '',
      tdoa: true,
      fdoa: true,
      reference: '',
      integration: '',
      step: '3',
    };
  });
  const [grid, setGrid] = useState<AccuracyGrid | null>(null);
  const [gridWhatIf, setGridWhatIf] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fieldErr, setFieldErr] = useState<Record<string, string>>({});
  const [field, setField] = useState<'semi_major_95_m' | 'cep50_m'>('semi_major_95_m');
  const [letters, setLetters] = useState(true);
  const [layers, setLayers] = useLayers('plan-acc', { sites: true, satellites: true, accuracy: true });
  const subs = subpointsResource.use();
  const u = cfg.processing?.uncertainty;
  // what-if uncertainty for this map only (AccuracyRequest.uncertainty); starts from the configured values
  const configuredU = useMemo(
    () => ({
      op: (u?.ephemeris?.operator?.pos_m ?? [100, 1000, 1000]).map(String),
      tle: (u?.ephemeris?.tle?.pos_m ?? [2000, 20000, 10000]).map(String),
      satOsc: String(u?.sat_osc_frac ?? 1e-6),
      siteOsc: String(u?.site_osc_frac ?? 1e-11),
      bias: String(u?.tdoa_bias_ns ?? 50),
      // propagation media (processing.uncertainty); a what-if must carry them, or the
      // server would take its defaults instead of the configured values
      vtec: String(u?.iono_vtec_tecu ?? 30),
      ionoRho: String(u?.iono_correlation ?? 0.5),
      tropo: String(u?.tropo_zenith_m ?? 2.4),
      tropoRho: String(u?.tropo_correlation ?? 0.9),
    }),
    [u],
  );
  const [whatIf, setWhatIf] = useState(false);
  const [wu, setWu] = useState(configuredU);
  useEffect(() => {
    if (!whatIf) setWu(configuredU);
  }, [configuredU, whatIf]);

  const feHz = parseMhzToHz(form.feMhz);
  const relaying = feHz ? transpondersRelaying(cfg, feHz) : [];
  const set = (patch: Partial<AccForm>) => setForm((f) => ({ ...f, ...patch }));

  const compute = async () => {
    const errs: Record<string, string> = {};
    if (!form.pairs.length) errs.pairs = 'Add at least one pair of channels.';
    form.pairs.forEach(([a, b], i) => {
      if (a.split('@')[0] === b.split('@')[0]) errs[`pairs.${i}`] = 'A pair needs two different channels.';
    });
    if (!(feHz && feHz > 0)) errs.f_e_hz = 'Enter the emitter uplink frequency in MHz.';
    if (!form.tdoa && !form.fdoa) errs.kinds = 'Choose TDOA, FDOA or both.';
    const bw = Number(form.bwKhz) * 1000;
    if (form.waveform !== 'cw' && !(bw > 0)) errs['emitter.bandwidth_hz'] = 'A bandwidth above 0 kHz is needed unless the waveform is a carrier (cw).';
    const num = (t: string) => (t.trim() === '' ? NaN : Number(t));
    if (whatIf && [...wu.op, ...wu.tle, wu.satOsc, wu.siteOsc, wu.bias, wu.vtec, wu.tropo].some((t) => !(num(t) >= 0))) errs.uncertainty = 'Every uncertainty must be a number of 0 or more.';
    if (whatIf && [wu.ionoRho, wu.tropoRho].some((t) => !(num(t) >= 0 && num(t) <= 1))) errs.uncertainty = 'A correlation must be a number from 0 to 1.';
    setFieldErr(errs);
    if (Object.keys(errs).length) return;
    const uncertainty = whatIf
      ? {
          ephemeris: {
            operator: { pos_m: wu.op.map(num), vel_mps: u?.ephemeris?.operator?.vel_mps ?? [0.02, 0.02, 0.05] },
            tle: { pos_m: wu.tle.map(num), vel_mps: u?.ephemeris?.tle?.vel_mps ?? [0.3, 0.3, 0.7] },
          },
          sat_osc_frac: num(wu.satOsc),
          site_osc_frac: num(wu.siteOsc),
          tdoa_bias_ns: num(wu.bias),
          iono_vtec_tecu: num(wu.vtec),
          iono_correlation: num(wu.ionoRho),
          tropo_zenith_m: num(wu.tropo),
          tropo_correlation: num(wu.tropoRho),
        }
      : null;
    const req: AccuracyRequest = {
      pairs: form.pairs,
      emitter: { eirp_dbw: Number(form.eirp), bandwidth_hz: form.waveform === 'cw' ? 0 : bw, waveform: form.waveform },
      f_e_hz: feHz ?? 0,
      step_deg: Number(form.step),
      kinds: [...(form.tdoa ? (['tdoa'] as const) : []), ...(form.fdoa ? (['fdoa'] as const) : [])],
      reference_carrier_id: form.reference || null,
      integration_s: form.integration ? Number(form.integration) : null,
      uncertainty,
    };
    setBusy(true);
    setError(null);
    try {
      setGrid(await api.accuracy(req));
      setGridWhatIf(uncertainty !== null);
    } catch (e) {
      if (e instanceof ValidationError) {
        const fe: Record<string, string> = {};
        for (const p of e.errors) fe[p.loc.split('.')[0] ?? ''] = p.msg;
        setFieldErr(fe);
        setError(e.errors.map((p) => p.msg).join(' '));
      } else setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const stats = useMemo(() => {
    if (!grid) return null;
    const vals: number[] = [];
    const dom = new Map<string, number>();
    let cells = 0;
    grid[field].forEach((row, i) =>
      row.forEach((v, j) => {
        cells += 1;
        if (v === null) return;
        vals.push(v);
        const d = grid.dominant[i]?.[j];
        if (d) dom.set(d, (dom.get(d) ?? 0) + 1);
      }),
    );
    vals.sort((a, b) => a - b);
    const top = [...dom.entries()].sort((a, b) => b[1] - a[1]);
    return { cells, n: vals.length, median: vals[Math.floor(vals.length / 2)] ?? null, best: vals[0] ?? null, top };
  }, [grid, field]);

  const data: MapData = useMemo(
    () => ({ sites: cfg.sites.map((s) => ({ id: s.id, name: s.name, lat: s.lat_deg, lon: s.lon_deg })), subpoints: subs.data ?? [], heat: grid ? { grid, field, letters } : null }),
    [cfg, subs.data, grid, field, letters],
  );

  // UX-13: the channel (or channel@sat) only, so the selects fit; the satellite is in the tooltip
  const chOpt = (c: PairEnd) => (
    <option key={c.value} value={c.value} title={`${c.channel} watching ${c.sat}`}>
      {c.value}
    </option>
  );
  const endTitle = (v: string) => {
    const e = ends.find((x) => x.value === v);
    return e ? `${e.channel} watching ${e.sat}` : v;
  };
  return (
    <div className="split">
      <form
        className="side"
        onSubmit={(e) => {
          e.preventDefault();
          void compute();
        }}
      >
        <div className="region">
          <h2>Satellite pairs</h2>
          {form.pairs.map(([a, b], i) => (
            <div key={i} className="field" style={{ marginBottom: 6 }}>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr auto 1fr auto', gap: 6, alignItems: 'center' }}>
                <select aria-label={`Pair ${i + 1} channel A`} title={endTitle(a)} value={a} onChange={(e) => set({ pairs: form.pairs.map((p, k) => (k === i ? [e.target.value, p[1]] : p)) })} aria-invalid={fieldErr[`pairs.${i}`] ? true : undefined}>
                  {ends.map(chOpt)}
                </select>
                <span aria-hidden="true">−</span>
                <select aria-label={`Pair ${i + 1} channel B`} title={endTitle(b)} value={b} onChange={(e) => set({ pairs: form.pairs.map((p, k) => (k === i ? [p[0], e.target.value] : p)) })}>
                  {ends.map(chOpt)}
                </select>
                <button type="button" className="btn small" aria-label={`Remove pair ${i + 1}`} onClick={() => set({ pairs: form.pairs.filter((_, k) => k !== i) })}>
                  Remove
                </button>
              </div>
              {fieldErr[`pairs.${i}`] ? <span className="err">{fieldErr[`pairs.${i}`]}</span> : null}
            </div>
          ))}
          {form.pairs.length === 0 && ends.length < 2 ? <p className="ink2">This configuration has fewer than two receive channels, so there is no pair to correlate.</p> : null}
          <button type="button" className="btn small" disabled={ends.length < 2} onClick={() => set({ pairs: [...form.pairs, [ends[0]?.value ?? '', ends.find((e) => e.channel !== ends[0]?.channel)?.value ?? ends[1]?.value ?? '']] })}>
            Add pair
          </button>
          {fieldErr.pairs ? <p className="err-text" role="alert">{fieldErr.pairs}</p> : null}
        </div>
        <div className="region">
          <h2>Emitter assumed</h2>
          <div className="fieldrow">
            <div className="field">
              <label htmlFor="acc-fe">
                Frequency <span className="unit">MHz</span>
              </label>
              <input id="acc-fe" className="mono" value={form.feMhz} onChange={(e) => set({ feMhz: e.target.value })} aria-invalid={fieldErr.f_e_hz ? true : undefined} />
              {fieldErr.f_e_hz ? <span className="err">{fieldErr.f_e_hz}</span> : <span className="hint">{relaying.length ? `relayed by ${relaying.map((r) => `${r.sat.id}/${r.tp.id}`).join(', ')}` : 'no transponder relays this'}</span>}
            </div>
            <div className="field">
              <label htmlFor="acc-eirp">
                EIRP <span className="unit">dBW</span>
              </label>
              <input id="acc-eirp" type="number" step="0.5" value={form.eirp} onChange={(e) => set({ eirp: e.target.value })} />
            </div>
            <div className="field">
              <label htmlFor="acc-wf">Waveform</label>
              <select id="acc-wf" value={form.waveform} onChange={(e) => set({ waveform: e.target.value as AccForm['waveform'] })}>
                <option value="noise">noise</option>
                <option value="psk">psk</option>
                <option value="chirp">chirp</option>
                <option value="cw">cw (carrier)</option>
              </select>
            </div>
            <div className="field">
              <label htmlFor="acc-bw">
                Bandwidth <span className="unit">kHz</span>
              </label>
              <input id="acc-bw" type="number" step="0.1" value={form.bwKhz} disabled={form.waveform === 'cw'} onChange={(e) => set({ bwKhz: e.target.value })} aria-invalid={fieldErr['emitter.bandwidth_hz'] ? true : undefined} />
              {fieldErr['emitter.bandwidth_hz'] ? <span className="err">{fieldErr['emitter.bandwidth_hz']}</span> : null}
            </div>
          </div>
        </div>
        <div className="region">
          <h2>Measurements and calibration</h2>
          <div className="fieldrow">
            <div className="field">
              <span className="label">Kinds</span>
              <span>
                <label>
                  <input type="checkbox" checked={form.tdoa} onChange={(e) => set({ tdoa: e.target.checked })} /> TDOA
                </label>{' '}
                <label>
                  <input type="checkbox" checked={form.fdoa} onChange={(e) => set({ fdoa: e.target.checked })} /> FDOA
                </label>
              </span>
              {fieldErr.kinds ? <span className="err">{fieldErr.kinds}</span> : null}
            </div>
            <div className="field">
              <label htmlFor="acc-ref">Reference emitter</label>
              <select id="acc-ref" value={form.reference} onChange={(e) => set({ reference: e.target.value })}>
                <option value="">none</option>
                {/* R7: each cell against the reference tasking would use for an emitter there (a carrier named "nearest" takes precedence) */}
                {refs.length && !refs.some((r) => r.id === NEAREST_REFERENCE) ? <option value={NEAREST_REFERENCE}>nearest to each cell</option> : null}
                {refs.map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.id}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label htmlFor="acc-int">
                Integration <span className="unit">s</span>
              </label>
              <input id="acc-int" type="number" step="0.1" min="0" placeholder={`${cfg.processing?.capture?.duration_s ?? 1} (configured)`} value={form.integration} onChange={(e) => set({ integration: e.target.value })} />
            </div>
            <div className="field">
              <label htmlFor="acc-step">
                Grid step <span className="unit">°</span>
              </label>
              <select id="acc-step" value={form.step} onChange={(e) => set({ step: e.target.value })}>
                {['1', '2', '3', '5'].map((v) => (
                  <option key={v}>{v}</option>
                ))}
              </select>
            </div>
          </div>
        </div>
        <div className="region">
          <h2>
            Uncertainty assumed{' '}
            <Info label="uncertainty assumptions">
              One-sigma systematic errors the prediction assumes. By default the configured values (Configure, Processing, Uncertainty); a what-if changes them for this map
              only. Satellites with their own ephemeris sigma and the site clocks keep their configured values.
            </Info>
          </h2>
          <label style={{ display: 'block', marginBottom: 6 }}>
            <input type="checkbox" checked={whatIf} onChange={(e) => setWhatIf(e.target.checked)} /> What-if: change them for this map only
          </label>
          {whatIf ? (
            <div className="whatif">
              {(
                [
                  ['op', 'Ephemeris, operator', 'm R / I / C'],
                  ['tle', 'Ephemeris, TLE', 'm R / I / C'],
                ] as const
              ).map(([key, label, unit]) => (
                <div className="field" key={key}>
                  <span className="label">
                    {label} <span className="unit">{unit}</span>
                  </span>
                  <span className="vec3">
                    {wu[key].map((v, k) => (
                      <input
                        key={k}
                        className="mono"
                        inputMode="decimal"
                        aria-label={`${label}, ${['radial', 'in-track', 'cross-track'][k]}, m`}
                        value={v}
                        onChange={(e) => setWu((w) => ({ ...w, [key]: w[key].map((x, j) => (j === k ? e.target.value : x)) }))}
                      />
                    ))}
                  </span>
                </div>
              ))}
              <div className="fieldrow">
                {(
                  [
                    ['satOsc', 'Satellite oscillator', 'fraction'],
                    ['siteOsc', 'Site oscillator', 'fraction'],
                    ['bias', 'TDOA bias', 'ns'],
                    ['vtec', 'Ionosphere, VTEC', 'TECU'],
                    ['ionoRho', 'Ionosphere, correlation', '0 to 1'],
                    ['tropo', 'Troposphere, zenith', 'm'],
                    ['tropoRho', 'Troposphere, correlation', '0 to 1'],
                  ] as const
                ).map(([key, label, unit]) => (
                  <div className="field" key={key}>
                    <label htmlFor={`acc-u-${key}`}>
                      {label} <span className="unit">{unit}</span>
                    </label>
                    <input id={`acc-u-${key}`} className="mono" inputMode="decimal" value={wu[key]} onChange={(e) => setWu((w) => ({ ...w, [key]: e.target.value }))} />
                  </div>
                ))}
              </div>
              <button type="button" className="btn small" onClick={() => setWu(configuredU)}>
                Back to the configured values
              </button>
              {fieldErr.uncertainty ? <span className="err">{fieldErr.uncertainty}</span> : null}
            </div>
          ) : (
            <dl className="facts">
              <dt>Ephemeris, operator</dt>
              <dd className="mono">{configuredU.op.join(' / ')} m RIC</dd>
              <dt>Ephemeris, TLE</dt>
              <dd className="mono">{configuredU.tle.join(' / ')} m RIC</dd>
              <dt>Satellite oscillator</dt>
              <dd className="mono">{configuredU.satOsc}</dd>
              <dt>Site oscillator</dt>
              <dd className="mono">{configuredU.siteOsc}</dd>
              <dt>TDOA bias</dt>
              <dd className="mono">{configuredU.bias} ns</dd>
              <dt>Ionosphere</dt>
              <dd className="mono">
                {configuredU.vtec} TECU, correlation {configuredU.ionoRho}
              </dd>
              <dt>Troposphere</dt>
              <dd className="mono">
                {configuredU.tropo} m zenith, correlation {configuredU.tropoRho}
              </dd>
            </dl>
          )}
          <p style={{ marginTop: 6 }}>
            <a href={href('configure', 'processing')}>Edit the configured values in Configure</a>
          </p>
        </div>
        <div className="region" style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
          <button type="submit" className="btn primary" disabled={busy}>
            {busy ? 'Computing…' : 'Compute accuracy map'}
          </button>
          {busy ? <span className="ink2">computing the grid</span> : null}
        </div>
        {error ? <ErrorNote error={error} /> : null}
        {grid && stats ? (
          <div className="region" aria-live="polite">
            <h2>Result</h2>
            {gridWhatIf ? <p className="notice">Computed with the what-if uncertainty above, not the configured values.</p> : null}
            {stats.n === 0 ? (
              <p>
                No cell can be located with these assumptions. One TDOA alone gives a line, not a position: add a pair through a third satellite, use FDOA, or a reference
                emitter.
              </p>
            ) : (
              <dl className="facts">
                <dt>Locatable</dt>
                <dd>
                  {stats.n} of {stats.cells} cells
                </dd>
                <dt>{field === 'cep50_m' ? 'CEP50' : '95 % error'}</dt>
                <dd className="mono">
                  best {km(stats.best)} km, median {km(stats.median)} km
                </dd>
                <dt>Dominant error</dt>
                <dd>
                  {stats.top
                    .slice(0, 3)
                    .map(([d, n]) => `${DOMINANT_WORD[d] ?? d} ${Math.round((100 * n) / stats.n)} %`)
                    .join(', ')}
                </dd>
              </dl>
            )}
            <div style={{ display: 'flex', gap: 12, marginTop: 8, flexWrap: 'wrap' }}>
              <span className="seg" role="group" aria-label="Shown value">
                <button type="button" aria-pressed={field === 'semi_major_95_m'} onClick={() => setField('semi_major_95_m')}>
                  95 % semi-major
                </button>
                <button type="button" aria-pressed={field === 'cep50_m'} onClick={() => setField('cep50_m')}>
                  CEP50
                </button>
              </span>
              <label>
                <input type="checkbox" checked={letters} onChange={(e) => setLetters(e.target.checked)} /> Dominant-error letters
              </label>
            </div>
            {letters ? (
              <p className="ink2" style={{ marginTop: 6 }}>
                Letters (drawn when cells are large enough; zoom in): {DOMINANT_KEY}.
              </p>
            ) : null}
          </div>
        ) : null}
      </form>
      <div className="main">
        <MapView label="Predicted location accuracy over the region" data={data} layers={layers} onLayers={setLayers} region={cfg.processing?.region ?? null}>
          {!grid && !busy ? <div className="mapnote">Set the assumptions and compute; the grid is drawn here.</div> : null}
        </MapView>
      </div>
    </div>
  );
}

export function PlanView() {
  const { data, error, reload } = configResource.use();
  const route = useRoute();
  const mode = route.params.get('mode') === 'accuracy' ? 'accuracy' : 'look';
  if (error && !data) return <ErrorNote error={`Could not load the configuration: ${error}`} retry={reload} />;
  if (!data) return <Loading what="the configuration" />;
  const cfg = data.envelope.config;
  if (!cfg.sites.length || !cfg.satellites.length) return <Empty title="Nothing to plan yet">Add satellites and sites under Configure first.</Empty>;
  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0, flex: 1 }}>
      <div className="toolbar">
        <span className="seg" role="group" aria-label="Planning tool">
          <button type="button" aria-pressed={mode === 'look'} onClick={() => setParams({ mode: null })}>
            Look angles and footprints
          </button>
          <button type="button" aria-pressed={mode === 'accuracy'} onClick={() => setParams({ mode: 'accuracy' })}>
            Accuracy map
          </button>
        </span>
      </div>
      <div style={{ flex: 1, minHeight: 0 }}>{mode === 'look' ? <LookAngles cfg={cfg} /> : <Accuracy cfg={cfg} />}</div>
    </div>
  );
}
