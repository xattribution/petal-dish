// Planning: who sees which satellite (look angles, footprints) and how well an emitter
// anywhere in the region would be located (accuracy map, MATH.md section 10).
import { useMemo, useState } from 'react';
import { ValidationError } from '../api/client';
import type { AccuracyGrid, AccuracyRequest, LookAngle, SystemConfig } from '../api/types.gen';
import { Empty, ErrorNote, Info, Loading, StatusSymbol } from '../components/common';
import { transpondersRelaying } from '../domain/config';
import { db, deg, fixed, km, mhz, parseMhzToHz } from '../format/units';
import { DOMINANT_WORD, MapView, type MapData } from '../map/MapView';
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
    return `${kind}, ${across !== null ? `${fixed(across, 2)} λ across` : 'under 3 λ'}: reflector`;
  };
  return (
    <table className="grid" aria-label="Look angles of every channel to every satellite">
      <thead>
        <tr>
          <th>Site</th>
          <th>Channel</th>
          <th>Satellite</th>
          <th>Watched</th>
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
            <td>{l.watched ? 'yes' : ''}</td>
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
  const channels = cfg.sites.flatMap((s) => s.channels.map((c) => ({ id: c.id, site: s.id, sat: 'satellite_id' in c.pointing ? c.pointing.satellite_id : c.pointing.satellite_ids.join('+') })));
  const refs = (cfg.carriers ?? []).filter((c) => c.use_as_reference);
  const [form, setForm] = useState<AccForm>({
    pairs: [
      ['naha-pac', 'naha-172e'],
      ['naha-io', 'naha-172e'],
    ].filter(([a, b]) => channels.some((c) => c.id === a) && channels.some((c) => c.id === b)) as Array<[string, string]>,
    eirp: '25',
    bwKhz: '8',
    waveform: 'noise',
    feMhz: '301.005',
    tdoa: true,
    fdoa: true,
    reference: '',
    integration: '',
    step: '3',
  });
  const [grid, setGrid] = useState<AccuracyGrid | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fieldErr, setFieldErr] = useState<Record<string, string>>({});
  const [field, setField] = useState<'semi_major_95_m' | 'cep50_m'>('semi_major_95_m');
  const [letters, setLetters] = useState(true);
  const [layers, setLayers] = useLayers('plan-acc', { sites: true, satellites: true, accuracy: true });
  const subs = subpointsResource.use();
  const u = cfg.processing?.uncertainty;

  const feHz = parseMhzToHz(form.feMhz);
  const relaying = feHz ? transpondersRelaying(cfg, feHz) : [];
  const set = (patch: Partial<AccForm>) => setForm((f) => ({ ...f, ...patch }));

  const compute = async () => {
    const errs: Record<string, string> = {};
    if (!form.pairs.length) errs.pairs = 'Add at least one pair of channels.';
    form.pairs.forEach(([a, b], i) => {
      if (a === b) errs[`pairs.${i}`] = 'A pair needs two different channels.';
    });
    if (!(feHz && feHz > 0)) errs.f_e_hz = 'Enter the emitter frequency in MHz, for example 301.005.';
    if (!form.tdoa && !form.fdoa) errs.kinds = 'Choose TDOA, FDOA or both.';
    const bw = Number(form.bwKhz) * 1000;
    if (form.waveform !== 'cw' && !(bw > 0)) errs['emitter.bandwidth_hz'] = 'A bandwidth above 0 kHz is needed unless the waveform is a carrier (cw).';
    setFieldErr(errs);
    if (Object.keys(errs).length) return;
    const req: AccuracyRequest = {
      pairs: form.pairs,
      emitter: { eirp_dbw: Number(form.eirp), bandwidth_hz: form.waveform === 'cw' ? 0 : bw, waveform: form.waveform },
      f_e_hz: feHz ?? 0,
      step_deg: Number(form.step),
      kinds: [...(form.tdoa ? (['tdoa'] as const) : []), ...(form.fdoa ? (['fdoa'] as const) : [])],
      reference_carrier_id: form.reference || null,
      integration_s: form.integration ? Number(form.integration) : null,
    };
    setBusy(true);
    setError(null);
    try {
      setGrid(await api.accuracy(req));
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

  const chOpt = (c: { id: string; site: string; sat: string }) => (
    <option key={c.id} value={c.id}>
      {c.id} ({c.sat})
    </option>
  );
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
                <select aria-label={`Pair ${i + 1} channel A`} value={a} onChange={(e) => set({ pairs: form.pairs.map((p, k) => (k === i ? [e.target.value, p[1]] : p)) })} aria-invalid={fieldErr[`pairs.${i}`] ? true : undefined}>
                  {channels.map(chOpt)}
                </select>
                <span aria-hidden="true">−</span>
                <select aria-label={`Pair ${i + 1} channel B`} value={b} onChange={(e) => set({ pairs: form.pairs.map((p, k) => (k === i ? [p[0], e.target.value] : p)) })}>
                  {channels.map(chOpt)}
                </select>
                <button type="button" className="btn small" aria-label={`Remove pair ${i + 1}`} onClick={() => set({ pairs: form.pairs.filter((_, k) => k !== i) })}>
                  Remove
                </button>
              </div>
              {fieldErr[`pairs.${i}`] ? <span className="err">{fieldErr[`pairs.${i}`]}</span> : null}
            </div>
          ))}
          <button type="button" className="btn small" onClick={() => set({ pairs: [...form.pairs, [channels[0]?.id ?? '', channels[1]?.id ?? '']] })}>
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
            <Info label="uncertainty assumptions">The prediction uses the configured one-sigma systematic errors. Change them under Configure, Processing, Uncertainty.</Info>
          </h2>
          <dl className="facts">
            <dt>Ephemeris, operator</dt>
            <dd className="mono">{(u?.ephemeris?.operator?.pos_m ?? [100, 1000, 1000]).join(' / ')} m RIC</dd>
            <dt>Ephemeris, TLE</dt>
            <dd className="mono">{(u?.ephemeris?.tle?.pos_m ?? [2000, 20000, 10000]).join(' / ')} m RIC</dd>
            <dt>Satellite oscillator</dt>
            <dd className="mono">{u?.sat_osc_frac ?? 1e-6}</dd>
            <dt>Site oscillator</dt>
            <dd className="mono">{u?.site_osc_frac ?? 1e-11}</dd>
            <dt>TDOA bias</dt>
            <dd className="mono">{u?.tdoa_bias_ns ?? 50} ns</dd>
          </dl>
          <p style={{ marginTop: 6 }}>
            <a href={href('configure', 'processing')}>Edit in Configure</a>
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
                Letters (drawn when cells are large enough; zoom in): N noise, P ephemeris position, V ephemeris velocity, C site clocks, O oscillators, B TDOA bias, R reference.
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
