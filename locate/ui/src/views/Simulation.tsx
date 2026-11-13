// Drive the simulation (only when the server runs a scenario): place, move and edit
// emitters on the map (a jammer aimed at a satellite, or a local interferer near a site),
// waveform, power and antenna, truth errors, presets, reset. Everything here is simulated
// and labeled so.
import { useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { ValidationError } from '../api/client';
import type { EmitterSpec, FieldProblem, LinkBudgetResponse, Preset, Scenario, SystemConfig } from '../api/types.gen';
import { Empty, ErrorNote, Info, Loading, SimTag } from '../components/common';
import { downlinkCenter, transpondersRelaying } from '../domain/config';
import { distanceM, formatLatLon, formatMgrs } from '../format/geo';
import { db, deg, fixed, km, mhz } from '../format/units';
import { MapView, type MapData } from '../map/MapView';
import { href } from '../router';
import { configResource, footprintsResource, subpointsResource, truthResource } from '../state/resources';
import { api, useLive } from '../state/store';
import { BoolF, ContainerProblems, EditorContext, FreqF, NumF, SelectF, setIn, TextF, Vec3F, type Path } from './configEditor';
import { useLayers } from './Watch';

type Placing = { mode: 'jammer' } | { mode: 'local' } | { mode: 'move'; index: number } | null;

function nearestSite(cfg: SystemConfig, lat: number, lon: number) {
  let best: { id: string; name: string; d: number; i: number } | null = null;
  cfg.sites.forEach((s, i) => {
    const d = distanceM(lat, lon, s.lat_deg, s.lon_deg);
    if (!best || d < best.d) best = { id: s.id, name: s.name, d, i };
  });
  return best as { id: string; name: string; d: number; i: number } | null;
}

function emitterBandwidth(e: EmitterSpec): number {
  const w = e.waveform;
  if (!w || w.type === 'cw') return 0;
  if (w.type === 'psk') return w.bandwidth_hz || (w.symbol_rate_hz ?? 0) * (1 + (w.rolloff ?? 0.35));
  return w.bandwidth_hz ?? 0;
}

function EmitterForm({ i, e, cfg }: { i: number; e: EmitterSpec; cfg: SystemConfig }) {
  const base: Path = ['emitters', i];
  const [budget, setBudget] = useState<LinkBudgetResponse | null>(null);
  const [budgetError, setBudgetError] = useState<string | null>(null);
  const relaying = typeof e.freq_hz === 'number' ? transpondersRelaying(cfg, e.freq_hz) : [];
  const pointing = e.pointing ?? 'omni';
  const pointMode = typeof pointing === 'string' ? 'omni' : 'satellite_id' in pointing ? 'sat' : 'azel';
  const wf = e.waveform ?? { type: 'noise' as const };
  const ant = e.antenna ?? { type: 'isotropic' as const };
  const near = typeof e.lat_deg === 'number' && typeof e.lon_deg === 'number' ? nearestSite(cfg, e.lat_deg, e.lon_deg) : null;
  return (
    <EditorField>
      {(update) => (
        <div className="cfgform" style={{ padding: 0 }}>
          <ContainerProblems path={base} />
          <div className="fieldrow">
            <TextF path={[...base, 'id']} label="Id" mono />
            <TextF path={[...base, 'name']} label="Name" optional />
            <SelectF path={[...base, 'kind']} label="Kind" options={[['jammer', 'Jammer (via satellites)'], ['local', 'Local interferer'], ['carrier', 'Authorized carrier']]} />
            <BoolF path={[...base, 'enabled']} label="Radiating" />
          </div>
          <div className="fieldrow">
            <NumF path={[...base, 'lat_deg']} label="Latitude" unit="°" />
            <NumF path={[...base, 'lon_deg']} label="Longitude" unit="°" />
            <NumF path={[...base, 'alt_m']} label="Altitude" unit="m" optional />
            <NumF path={[...base, 'antenna_height_m']} label="Antenna height" unit="m" optional />
          </div>
          {typeof e.lat_deg === 'number' && typeof e.lon_deg === 'number' ? (
            <p className="mono ink2">
              {formatLatLon(e.lat_deg, e.lon_deg, 4)} WGS 84, MGRS {formatMgrs(e.lat_deg, e.lon_deg) ?? '—'}
              {near ? `, ${km(near.d)} km from ${near.id}` : ''}
            </p>
          ) : null}
          {e.kind === 'local' && near && near.d > 30000 ? (
            <div className="notice caution">A local interferer reaches a site only inside the radio horizon (about 26 km for 10 m antennas); {near.id} is {km(near.d)} km away.</div>
          ) : null}
          <div className="fieldrow">
            <FreqF path={[...base, 'freq_hz']} label={e.kind === 'local' ? 'Frequency radiated (downlink band)' : 'Frequency radiated (uplink)'} hint={e.kind !== 'local' ? (relaying.length ? `relayed by ${relaying.map((r) => `${r.sat.id}/${r.tp.id}`).join(', ')}` : 'no transponder relays this frequency') : undefined} />
            <NumF path={[...base, 'eirp_dbw']} label="EIRP" unit="dBW" />
          </div>
          <fieldset>
            <legend>Waveform</legend>
            <div className="fieldrow">
              <SelectF path={[...base, 'waveform', 'type']} label="Type" options={[['noise', 'noise'], ['chirp', 'chirp (swept)'], ['psk', 'psk'], ['cw', 'cw (carrier)']]} />
              {wf.type === 'noise' || wf.type === 'chirp' ? <NumF path={[...base, 'waveform', 'bandwidth_hz']} label="Bandwidth" unit="kHz" scale={1000} /> : null}
              {wf.type === 'chirp' ? <NumF path={[...base, 'waveform', 'sweep_period_s']} label="Sweep period" unit="ms" scale={1e-3} optional /> : null}
              {wf.type === 'psk' ? (
                <>
                  <NumF path={[...base, 'waveform', 'symbol_rate_hz']} label="Symbol rate" unit="sym/s" />
                  <SelectF path={[...base, 'waveform', 'order']} label="Order" options={[['2', 'BPSK'], ['4', 'QPSK'], ['8', '8PSK']]} />
                  <NumF path={[...base, 'waveform', 'rolloff']} label="Roll-off" optional />
                </>
              ) : null}
              <NumF path={[...base, 'waveform', 'duty']} label="Duty" optional />
              <NumF path={[...base, 'waveform', 'period_s']} label="Gate period" unit="s" optional />
            </div>
          </fieldset>
          <fieldset>
            <legend>Antenna and pointing</legend>
            <span className="seg" role="group" aria-label="Emitter antenna">
              {[
                ['isotropic', 'Isotropic'],
                ['beam', 'Beam'],
                ['parabolic', 'Parabolic'],
              ].map(([t, l]) => (
                <button
                  key={t}
                  type="button"
                  aria-pressed={ant.type === t}
                  onClick={() => update([...base, 'antenna'], t === 'isotropic' ? { type: 'isotropic', gain_dbi: 0 } : t === 'beam' ? { type: 'beam', gain_dbi: 10, hpbw_deg: 52, front_to_back_db: 20 } : { type: 'parabolic', diameter_m: 1.8, efficiency: 0.55 })}
                >
                  {l}
                </button>
              ))}
            </span>
            <div className="fieldrow">
              {ant.type === 'beam' ? (
                <>
                  <NumF path={[...base, 'antenna', 'gain_dbi']} label="Gain" unit="dBi" />
                  <NumF path={[...base, 'antenna', 'hpbw_deg']} label="Beamwidth" unit="°" />
                </>
              ) : null}
              {ant.type === 'parabolic' ? <NumF path={[...base, 'antenna', 'diameter_m']} label="Diameter" unit="m" /> : null}
            </div>
            <span className="seg" role="group" aria-label="Emitter pointing">
              <button type="button" aria-pressed={pointMode === 'omni'} onClick={() => update([...base, 'pointing'], 'omni')}>
                Omni
              </button>
              <button type="button" aria-pressed={pointMode === 'sat'} onClick={() => update([...base, 'pointing'], { satellite_id: cfg.satellites[0]?.id ?? '' })}>
                Aimed at a satellite
              </button>
              <button type="button" aria-pressed={pointMode === 'azel'} onClick={() => update([...base, 'pointing'], { az_deg: 90, el_deg: 30 })}>
                Fixed az / el
              </button>
            </span>
            {pointMode === 'sat' ? <SelectF path={[...base, 'pointing', 'satellite_id']} label="Aimed at" options={cfg.satellites.map((s) => [s.id, s.id])} /> : null}
            {pointMode === 'azel' ? (
              <div className="fieldrow">
                <NumF path={[...base, 'pointing', 'az_deg']} label="Azimuth" unit="°" />
                <NumF path={[...base, 'pointing', 'el_deg']} label="Elevation" unit="°" />
              </div>
            ) : null}
          </fieldset>
          <fieldset>
            <legend>On and off times (UTC, ISO 8601; empty = always)</legend>
            <div className="fieldrow">
              <TextF path={[...base, 't_start']} label="Start" mono optional />
              <TextF path={[...base, 't_stop']} label="Stop" mono optional />
            </div>
          </fieldset>
          {e.kind !== 'local' ? (
            <fieldset>
              <legend>
                Predicted paths <SimTag />
              </legend>
              <div>
                <button
                  type="button"
                  className="btn small"
                  onClick={async () => {
                    setBudgetError(null);
                    try {
                      setBudget(
                        await api.linkBudget({
                          emitter: { eirp_dbw: e.eirp_dbw ?? 0, bandwidth_hz: emitterBandwidth(e), waveform: wf.type, antenna: ant, pointing },
                          f_e_hz: e.freq_hz ?? 0,
                          lat_deg: e.lat_deg ?? 0,
                          lon_deg: e.lon_deg ?? 0,
                          alt_m: e.alt_m ?? 0,
                        }),
                      );
                    } catch (err) {
                      setBudget(null);
                      setBudgetError(err instanceof ValidationError ? err.errors.map((p) => p.msg).join(' ') : (err as Error).message);
                    }
                  }}
                >
                  Compute link budget
                </button>
              </div>
              {budgetError ? <p className="err-text">Link budget not available: {budgetError}</p> : null}
              {budget ? (
                budget.rows.length ? (
                  <div className="tablewrap" style={{ maxHeight: 260 }}>
                    <table className="grid">
                      <thead>
                        <tr>
                          <th>Satellite</th>
                          <th>Channel</th>
                          <th className="num">
                            Up el <span className="unit">°</span>
                          </th>
                          <th className="num">
                            C/N0 <span className="unit">dBHz</span>
                          </th>
                          <th className="num">
                            SNR <span className="unit">dB</span>
                          </th>
                          <th>Detectable</th>
                        </tr>
                      </thead>
                      <tbody>
                        {[...budget.rows].sort((a, b) => b.snr_db - a.snr_db).map((r) => (
                          <tr key={`${r.sat_id}|${r.transponder_id}|${r.channel_id}`} className={r.detectable ? undefined : 'muted'}>
                            <td className="mono">
                              {r.sat_id}/{r.transponder_id}
                            </td>
                            <td className="mono">{r.channel_id}</td>
                            <td className="num">{deg(r.uplink_elevation_deg)}</td>
                            <td className="num">{db(r.cn0_total_dbhz)}</td>
                            <td className="num">{db(r.snr_db)}</td>
                            <td>{r.detectable ? 'yes' : 'no'}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ) : (
                  <p className="ink2">No path exists: no transponder relays this frequency from here, or no satellite is above both horizons.</p>
                )
              ) : null}
            </fieldset>
          ) : null}
        </div>
      )}
    </EditorField>
  );
}

function EditorField({ children }: { children: (update: (p: Path, v: unknown) => void) => ReactNode }) {
  const ctx = useContext(EditorContext);
  return <>{children(ctx.update)}</>;
}

function TruthErrors({ s, cfg }: { s: Scenario; cfg: SystemConfig }) {
  const ctx = useContext(EditorContext);
  const errs = s.errors ?? {};
  return (
    <div className="cfgform" style={{ padding: 0 }}>
      <fieldset>
        <legend>Ephemeris offsets (truth minus believed)</legend>
        {cfg.satellites.map((sat) => {
          const has = !!errs.ephemeris?.[sat.id];
          return (
            <div key={sat.id}>
              <label>
                <input
                  type="checkbox"
                  checked={has}
                  onChange={(e) => ctx.update(['errors', 'ephemeris', sat.id], e.target.checked ? { ric_pos_m: [0, 500, 0], ric_vel_mps: [0, 0, 0] } : undefined)}
                />{' '}
                <span className="mono">{sat.id}</span>
              </label>
              {has ? (
                <div className="fieldrow" style={{ gridTemplateColumns: '1fr 1fr' }}>
                  <Vec3F path={['errors', 'ephemeris', sat.id, 'ric_pos_m']} label="Position offset" unit="m" />
                  <Vec3F path={['errors', 'ephemeris', sat.id, 'ric_vel_mps']} label="Velocity offset" unit="m/s" />
                </div>
              ) : null}
            </div>
          );
        })}
      </fieldset>
      <fieldset>
        <legend>Oscillators and clocks</legend>
        <div className="fieldrow">
          {cfg.satellites.map((sat) => (
            <NumF key={sat.id} path={['errors', 'sat_osc_frac', sat.id]} label={`${sat.id} translation oscillator`} unit="fraction" optional placeholder="0 (exact)" />
          ))}
        </div>
        <div className="fieldrow">
          {cfg.sites.map((site) => (
            <NumF key={site.id} path={['errors', 'site_clock_offset_ns', site.id]} label={`${site.id} clock offset`} unit="ns" optional placeholder="0 (exact)" />
          ))}
        </div>
        <div className="fieldrow">
          {cfg.sites.map((site) => (
            <NumF key={site.id} path={['errors', 'site_osc_frac', site.id]} label={`${site.id} oscillator`} unit="fraction" optional placeholder="0 (exact)" />
          ))}
        </div>
      </fieldset>
      <fieldset>
        <legend>Propagation media</legend>
        <div className="fieldrow">
          <BoolF path={['errors', 'iono', 'enabled']} label="Apply ionospheric delay" />
          <NumF path={['errors', 'iono', 'vtec_tecu']} label="Vertical TEC" unit="TECU" optional />
          <BoolF path={['errors', 'tropo', 'enabled']} label="Apply tropospheric delay" />
          <NumF path={['errors', 'tropo', 'zenith_m']} label="Zenith delay" unit="m" optional />
        </div>
      </fieldset>
    </div>
  );
}

export function SimulationView() {
  const simulation = useLive((s) => s.meta?.simulation);
  const simVersion = useLive((s) => s.simVersion);
  const cfgRes = configResource.use();
  const subs = subpointsResource.use();
  const fps = footprintsResource(0).use();
  const truth = truthResource.use(simulation === true);
  const [base, setBase] = useState<{ version: number; scenario: Scenario } | null>(null);
  const [draft, setDraft] = useState<Scenario | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [presets, setPresets] = useState<Preset[]>([]);
  const [selected, setSelected] = useState<number | null>(null);
  const [placing, setPlacing] = useState<Placing>(null);
  const [errors, setErrors] = useState<FieldProblem[]>([]);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ kind: 'ok' | 'error'; text: string } | null>(null);
  const [confirmReset, setConfirmReset] = useState(false);
  const [confirmPreset, setConfirmPreset] = useState(false);
  const [preset, setPreset] = useState('');
  const [layers, setLayers] = useLayers('sim', { sites: true, satellites: true, footprints: false, truth: true });
  const cfg = cfgRes.data?.envelope.config ?? null;

  const load = async () => {
    try {
      const [env, ps] = await Promise.all([api.scenario(), api.presets()]);
      setBase(env);
      setDraft(structuredClone(env.scenario));
      setPresets(ps);
      setLoadError(null);
    } catch (e) {
      setLoadError((e as Error).message);
    }
  };
  useEffect(() => {
    if (simulation) void load();
  }, [simulation]);
  // another console changed the scenario: reload when we have no unsaved edits
  const dirty = !!draft && !!base && JSON.stringify(draft) !== JSON.stringify(base.scenario);
  useEffect(() => {
    if (simVersion !== null && base && simVersion !== base.version && !dirty) void load();
  }, [simVersion]);

  const update = (path: Path, value: unknown) => {
    setDraft((d) => (d ? setIn(d, path, value) : d));
    setMessage(null);
  };

  const apply = async (s: Scenario) => {
    setBusy(true);
    setMessage(null);
    try {
      const env = await api.putScenario(s);
      setBase(env);
      setDraft(structuredClone(env.scenario));
      setErrors([]);
      setMessage({ kind: 'ok', text: `Applied as scenario version ${env.version}; nodes switch to it within 2 s` });
      void truthResource.load();
    } catch (e) {
      if (e instanceof ValidationError) {
        setErrors(e.errors.map((p) => ({ loc: p.loc.replace(/^scenario\./, ''), msg: p.msg })));
        setMessage({ kind: 'error', text: `Not applied: ${e.errors.length} problem${e.errors.length === 1 ? '' : 's'}` });
      } else setMessage({ kind: 'error', text: `Not applied: ${(e as Error).message}` });
    } finally {
      setBusy(false);
    }
  };

  const loadPreset = async () => {
    setConfirmPreset(false);
    setBusy(true);
    try {
      const env = await api.loadPreset(preset);
      setBase(env);
      setDraft(structuredClone(env.scenario));
      setSelected(null);
      setMessage({ kind: 'ok', text: `Loaded preset ${preset} as version ${env.version}` });
      void truthResource.load();
    } catch (e) {
      setMessage({ kind: 'error', text: (e as Error).message });
    } finally {
      setBusy(false);
    }
  };

  const onMapClick = (lat: number, lon: number) => {
    if (!draft || !cfg || !placing) return;
    lat = Math.round(lat * 1e4) / 1e4;
    lon = Math.round(lon * 1e4) / 1e4;
    if (placing.mode === 'move') {
      update(['emitters', placing.index, 'lat_deg'], lat);
      update(['emitters', placing.index, 'lon_deg'], lon);
      setPlacing(null);
      return;
    }
    const n = draft.emitters?.length ?? 0;
    const used = new Set((draft.emitters ?? []).map((e) => e.id));
    const id = (() => {
      for (let k = 1; ; k++) if (!used.has(`${placing.mode}-${k}`)) return `${placing.mode}-${k}`;
    })();
    let spec: EmitterSpec;
    if (placing.mode === 'jammer') {
      // aim at the satellite whose sub-point is nearest
      const sp = (subs.data ?? []).slice().sort((a, b) => distanceM(lat, lon, a.lat_deg, a.lon_deg) - distanceM(lat, lon, b.lat_deg, b.lon_deg))[0];
      const tp = cfg.satellites.find((s) => s.id === sp?.sat_id)?.transponders[0] ?? cfg.satellites[0]?.transponders[0];
      spec = {
        id,
        name: `Jammer placed at ${formatLatLon(lat, lon, 2)}`,
        kind: 'jammer',
        lat_deg: lat,
        lon_deg: lon,
        alt_m: 0,
        antenna_height_m: 10,
        freq_hz: (tp?.uplink_center_hz ?? 301e6) + 5000,
        eirp_dbw: 25,
        antenna: { type: 'beam', gain_dbi: 10, hpbw_deg: 52, front_to_back_db: 20 },
        pointing: { satellite_id: sp?.sat_id ?? cfg.satellites[0]?.id ?? '' },
        waveform: { type: 'noise', bandwidth_hz: 8000 },
        enabled: true,
      };
    } else {
      const near = nearestSite(cfg, lat, lon);
      const ch = near ? cfg.sites[near.i]?.channels[0] : undefined;
      const satId = ch ? ('satellite_id' in ch.pointing ? ch.pointing.satellite_id : ch.pointing.satellite_ids[0]) : undefined;
      const tp = cfg.satellites.find((s) => s.id === satId)?.transponders.at(-1);
      spec = {
        id,
        name: near ? `Local interferer near ${near.id}` : 'Local interferer',
        kind: 'local',
        lat_deg: lat,
        lon_deg: lon,
        alt_m: 0,
        antenna_height_m: 10,
        freq_hz: tp ? downlinkCenter(tp) + 2500 : 260.5e6,
        eirp_dbw: -20,
        antenna: { type: 'isotropic', gain_dbi: 0 },
        pointing: 'omni',
        waveform: { type: 'cw' },
        enabled: true,
      };
    }
    update(['emitters'], [...(draft.emitters ?? []), spec]);
    setSelected(n);
    setPlacing(null);
  };

  const data: MapData = useMemo(() => {
    const ems = draft?.emitters ?? [];
    return {
      sites: cfg?.sites.map((s) => ({ id: s.id, name: s.name, lat: s.lat_deg, lon: s.lon_deg })) ?? [],
      subpoints: subs.data ?? [],
      footprints: fps.data ?? [],
      truth: ems
        .map((e, i) => ({ e, i }))
        .filter(({ e }) => typeof e.lat_deg === 'number' && typeof e.lon_deg === 'number')
        .map(({ e, i }) => ({ id: String(i), lat: e.lat_deg ?? 0, lon: e.lon_deg ?? 0, label: e.id, selected: selected === i, enabled: e.enabled !== false })),
    };
  }, [draft, cfg, subs.data, fps.data, selected]);

  if (simulation === false) return <Empty title="Not simulating">This server runs without a scenario, so there is nothing to drive. Start it with a scenario (petal-locate demo) to use this screen.</Empty>;
  if (loadError) return <ErrorNote error={`Could not read the scenario: ${loadError}`} retry={() => void load()} />;
  if (!draft || !base || !cfg) return <Loading what="the scenario" />;
  const ems = draft.emitters ?? [];
  const sel = selected !== null ? ems[selected] : undefined;
  return (
    <EditorContext.Provider value={{ doc: draft, update, errors, warnings: [] }}>
      <div style={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0, flex: 1 }}>
        <div className="simhead">
          <SimTag />
          <strong>{draft.name}</strong>
          <span className="ink2">
            version {base.version}
            {dirty ? ', unapplied changes' : ''}
          </span>
          <span style={{ flex: 1 }} />
          <label>
            Preset{' '}
            <select value={preset} onChange={(e) => setPreset(e.target.value)} aria-label="Preset">
              <option value="">choose</option>
              {presets.map((p) => (
                <option key={p.name} value={p.name} title={p.description}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>
          {confirmPreset ? (
            // UX-12: loading a preset over unapplied edits asks first, as Reset does
            <>
              <span>Load {preset}? Unapplied changes are lost.</span>
              <button type="button" className="btn primary" disabled={busy} onClick={() => void loadPreset()}>
                Load {preset}
              </button>
              <button type="button" className="btn" onClick={() => setConfirmPreset(false)}>
                Cancel
              </button>
            </>
          ) : (
            <button type="button" className="btn" disabled={!preset || busy} onClick={() => (dirty ? setConfirmPreset(true) : void loadPreset())}>
              Load preset
            </button>
          )}
          {confirmReset ? (
            <>
              <span>Reset to the scenario the server started with? Unapplied changes are lost.</span>
              <button
                type="button"
                className="btn primary"
                onClick={async () => {
                  setConfirmReset(false);
                  try {
                    const env = await api.resetSim();
                    setBase(env);
                    setDraft(structuredClone(env.scenario));
                    setSelected(null);
                    setMessage({ kind: 'ok', text: `Reset to the start scenario, version ${env.version}` });
                    void truthResource.load();
                  } catch (e) {
                    setMessage({ kind: 'error', text: (e as Error).message });
                  }
                }}
              >
                Reset scenario
              </button>
              <button type="button" className="btn" onClick={() => setConfirmReset(false)}>
                Cancel
              </button>
            </>
          ) : (
            <button type="button" className="btn" onClick={() => setConfirmReset(true)}>
              Reset
            </button>
          )}
          <button type="button" className="btn" disabled={!dirty || busy} onClick={() => setDraft(structuredClone(base.scenario))}>
            Discard changes
          </button>
          <button type="button" className="btn primary" disabled={!dirty || busy} onClick={() => void apply(draft)}>
            Apply to the simulation
          </button>
        </div>
        {message ? (
          <div className={`notice${message.kind === 'error' ? ' critical' : ''}`} role={message.kind === 'error' ? 'alert' : 'status'} style={{ margin: 0 }}>
            {message.text}
          </div>
        ) : null}
        {errors.length ? (
          <ul className="problems">
            {errors.map((e, i) => (
              <li key={i}>
                <span className="mono">{e.loc}</span>: <span className="err-text">{e.msg}</span>
              </li>
            ))}
          </ul>
        ) : null}
        <div className="split" style={{ flex: 1, minHeight: 0, gridTemplateColumns: '460px 1fr' }}>
          <div className="side">
            <div className="region">
              <h2>
                Emitters <span className="aside">{ems.length} in the scenario</span>
              </h2>
              <div style={{ display: 'flex', gap: 8, marginBottom: 8, flexWrap: 'wrap' }}>
                <button type="button" className="btn" aria-pressed={placing?.mode === 'jammer'} onClick={() => setPlacing(placing?.mode === 'jammer' ? null : { mode: 'jammer' })}>
                  {placing?.mode === 'jammer' ? 'Click the map to place (Esc cancels)' : 'Place jammer'}
                </button>
                <button type="button" className="btn" aria-pressed={placing?.mode === 'local'} onClick={() => setPlacing(placing?.mode === 'local' ? null : { mode: 'local' })}>
                  {placing?.mode === 'local' ? 'Click near a site (Esc cancels)' : 'Place local interferer'}
                </button>
              </div>
              <table className="grid">
                <thead>
                  <tr>
                    <th>Id</th>
                    <th>Kind</th>
                    <th className="num">
                      Freq <span className="unit">MHz</span>
                    </th>
                    <th className="num">
                      EIRP <span className="unit">dBW</span>
                    </th>
                    <th>On</th>
                  </tr>
                </thead>
                <tbody>
                  {ems.map((e, i) => (
                    <tr key={i} aria-selected={selected === i} onClick={() => setSelected(i)} style={{ cursor: 'pointer' }}>
                      <td>
                        <button type="button" className="linkbtn mono" onClick={() => setSelected(i)}>
                          {e.id}
                        </button>
                      </td>
                      <td>{e.kind}</td>
                      <td className="num">{mhz(e.freq_hz ?? null, 4)}</td>
                      <td className="num">{fixed(e.eirp_dbw ?? null, 1)}</td>
                      <td>
                        <input type="checkbox" aria-label={`${e.id} radiating`} checked={e.enabled !== false} onChange={(ev) => update(['emitters', i, 'enabled'], ev.target.checked)} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {sel && selected !== null ? (
              <div className="region">
                <h2>
                  <span className="mono">{sel.id}</span>
                  <span className="aside">
                    <button type="button" className="linkbtn" onClick={() => setPlacing({ mode: 'move', index: selected })}>
                      {placing?.mode === 'move' ? 'click the new position' : 'Move on the map'}
                    </button>
                    {'  '}
                    <button
                      type="button"
                      className="linkbtn"
                      onClick={() => {
                        update(['emitters'], ems.filter((_, k) => k !== selected));
                        setSelected(null);
                      }}
                    >
                      Remove
                    </button>
                  </span>
                </h2>
                <EmitterForm i={selected} e={sel} cfg={cfg} />
              </div>
            ) : null}
            <div className="region">
              <h2>
                Truth errors <Info label="truth errors">How the simulated world differs from what the system believes. The solver does not know these values; they show how far a fix moves when the world is not as believed.</Info>
              </h2>
              <TruthErrors s={draft} cfg={cfg} />
            </div>
            <div className="region">
              <h2>
                Miss distance <SimTag />
              </h2>
              {truth.data ? (
                truth.data.events.length ? (
                  <table className="grid">
                    <thead>
                      <tr>
                        <th>Event</th>
                        <th>Emitter</th>
                        <th className="num">
                          Miss <span className="unit">km</span>
                        </th>
                        <th>In 95 % ellipse</th>
                      </tr>
                    </thead>
                    <tbody>
                      {truth.data.events.map((t) => (
                        <tr key={t.event_id}>
                          <td>
                            <a href={href('event', t.event_id)} className="mono">
                              {t.event_id}
                            </a>
                          </td>
                          <td className="mono">{t.emitter_id ?? '—'}</td>
                          <td className="num">{km(t.miss_m)}</td>
                          <td>{t.inside_ellipse === null ? '—' : t.inside_ellipse ? 'yes' : 'no'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                ) : (
                  <p className="ink2">No active event.</p>
                )
              ) : truth.error ? (
                <p className="err-text">{truth.error}</p>
              ) : (
                <Loading what="truth" />
              )}
            </div>
          </div>
          <div className="main">
            <MapView
              label="Simulation map: click to place or move emitters"
              data={data}
              layers={layers}
              onLayers={setLayers}
              region={cfg.processing?.region ?? null}
              placing={placing !== null}
              onMapClick={placing ? onMapClick : undefined}
              onPick={(kind, id) => kind === 'truth' && setSelected(Number(id))}
              truthLabel="Emitters (SIMULATION)"
            >
              <div className="mapnote">
                <SimTag /> {placing ? (placing.mode === 'move' ? 'Click the new position, or Enter for the map center' : 'Click to place, or Enter for the map center') : 'Click an emitter to edit it'}
              </div>
            </MapView>
          </div>
        </div>
      </div>
      <EscCancel active={placing !== null} onCancel={() => setPlacing(null)} />
    </EditorContext.Provider>
  );
}

function EscCancel({ active, onCancel }: { active: boolean; onCancel: () => void }) {
  useEffect(() => {
    if (!active) return;
    const k = (e: KeyboardEvent) => e.key === 'Escape' && onCancel();
    window.addEventListener('keydown', k);
    return () => window.removeEventListener('keydown', k);
  }, [active, onCancel]);
  return null;
}
