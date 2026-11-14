// Configure without code: satellites (TLE, geostationary slot, Kepler), transponders,
// sites, channels, antennas, pointing, monitor windows, carriers and reference emitters,
// processing settings. Field problems come from the server (POST /config/validate while
// editing, the 422 of PUT /config on save) and land on the field. Saving sends If-Match;
// a 409 opens a resolution panel. YAML import and export of the whole configuration.
import { useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { parse as parseYaml, stringify as toYaml } from 'yaml';
import { ConflictError, ValidationError } from '../api/client';
import type { FieldProblem, ReceiveChannel, Satellite, SystemConfig } from '../api/types.gen';
import { ErrorNote, Info, Loading } from '../components/common';
import { electricallySmall, monitorsOf } from '../domain/config';
import { formatLatLon, formatMgrs } from '../format/geo';
import { fixed, khz, mhz } from '../format/units';
import { href, navigate, useRoute } from '../router';
import { configResource } from '../state/resources';
import { api, useLive } from '../state/store';
import { dtg, isoFileStamp } from '../time/dtg';
import { knownZone } from '../time/zones';
import { BoolF, ContainerProblems, diffPaths, EditorContext, FreqF, NumF, SelectF, setIn, TextF, Vec3F, type Path } from './configEditor';

type Section = { kind: 'general' } | { kind: 'sat'; i: number } | { kind: 'site'; i: number } | { kind: 'carriers' } | { kind: 'processing' } | { kind: 'yaml' };

function parseSection(id: string | null): Section {
  if (!id || id === 'general') return { kind: 'general' };
  const m = id.match(/^(sat|site)-(\d+)$/);
  if (m) return { kind: m[1] as 'sat' | 'site', i: Number(m[2]) };
  if (id === 'carriers' || id === 'processing' || id === 'yaml') return { kind: id };
  return { kind: 'general' };
}
function sectionId(s: Section): string {
  return s.kind === 'sat' || s.kind === 'site' ? `${s.kind}-${s.i}` : s.kind;
}
function sectionOf(loc: string): string {
  const [a, b] = loc.split('.');
  if (a === 'satellites' && b !== undefined) return `sat-${b}`;
  if (a === 'sites' && b !== undefined) return `site-${b}`;
  if (a === 'carriers') return 'carriers';
  if (a === 'processing') return 'processing';
  return 'general';
}

export function tleChecksum(line: string): number {
  let s = 0;
  for (const ch of line.slice(0, 68)) {
    if (ch >= '0' && ch <= '9') s += Number(ch);
    else if (ch === '-') s += 1;
  }
  return s % 10;
}

function tleStatus(line: unknown, n: '1' | '2'): string {
  if (typeof line !== 'string' || !line) return '';
  const l = line.trimEnd();
  if (l.length !== 69) return `${l.length} of 69 characters`;
  if (l[0] !== n) return `must start with ${n}`;
  return Number(l[68]) === tleChecksum(l) ? 'checksum ok' : `checksum should be ${tleChecksum(l)}`;
}

// ------------------------------------------------------------------ satellites

function OrbitEditor({ base, sat }: { base: Path; sat: Satellite }) {
  const [paste, setPaste] = useState('');
  const ctxUpdate = useContextUpdate();
  const orbit = sat.orbit;
  const setType = (t: string) => {
    const now = new Date().toISOString().replace(/\.\d+Z$/, 'Z');
    const fresh: Record<string, Record<string, unknown>> = {
      geo: { type: 'geo', lon_deg: 0, inclination_deg: 0, epoch: now, phase_deg: 0, drift_deg_per_day: 0 },
      tle: { type: 'tle', line1: '', line2: '' },
      kepler: { type: 'kepler', a_m: 42164172.37, e: 0, inc_deg: 0, raan_deg: 0, argp_deg: 0, mean_anomaly_deg: 0, epoch: now, j2: true },
    };
    if (fresh[t]) ctxUpdate([...base, 'orbit'], fresh[t]);
  };
  return (
    <fieldset>
      <legend>Orbit</legend>
      <span className="seg" role="group" aria-label="Orbit definition">
        {[
          ['geo', 'Geostationary slot'],
          ['tle', 'TLE'],
          ['kepler', 'Kepler elements'],
        ].map(([t, l]) => (
          <button key={t} type="button" aria-pressed={orbit.type === t} onClick={() => orbit.type !== t && setType(t ?? '')}>
            {l}
          </button>
        ))}
        {orbit.type === 'table' ? <button type="button" aria-pressed>Ephemeris table</button> : null}
      </span>
      <ContainerProblems path={[...base, 'orbit']} />
      {orbit.type === 'geo' ? (
        <div className="fieldrow">
          <NumF path={[...base, 'orbit', 'lon_deg']} label="Slot longitude" unit="° E" />
          <NumF path={[...base, 'orbit', 'inclination_deg']} label="Inclination" unit="°" />
          <NumF path={[...base, 'orbit', 'phase_deg']} label="Phase at epoch" unit="°" />
          <NumF path={[...base, 'orbit', 'drift_deg_per_day']} label="Drift" unit="° / day" />
          <TextF path={[...base, 'orbit', 'epoch']} label="Epoch (UTC, ISO 8601)" mono />
        </div>
      ) : null}
      {orbit.type === 'tle' ? (
        <>
          <div className="field">
            <label htmlFor={`${base.join('.')}-tle-paste`}>Paste a TLE (two lines, or three with a name)</label>
            <textarea
              id={`${base.join('.')}-tle-paste`}
              className="mono"
              rows={3}
              value={paste}
              onChange={(e) => {
                setPaste(e.target.value);
                const lines = e.target.value.split(/\r?\n/).map((l) => l.trimEnd()).filter(Boolean);
                const l1 = lines.find((l) => l.startsWith('1 '));
                const l2 = lines.find((l) => l.startsWith('2 '));
                if (l1) ctxUpdate([...base, 'orbit', 'line1'], l1);
                if (l2) ctxUpdate([...base, 'orbit', 'line2'], l2);
                if (l1 && !sat.norad_id) {
                  const n = Number(l1.slice(2, 7));
                  if (Number.isInteger(n)) ctxUpdate([...base, 'norad_id'], n);
                }
              }}
            />
          </div>
          <TextF path={[...base, 'orbit', 'line1']} label="Line 1" mono wide hint={tleStatus(orbit.line1, '1')} />
          <TextF path={[...base, 'orbit', 'line2']} label="Line 2" mono wide hint={tleStatus(orbit.line2, '2')} />
        </>
      ) : null}
      {orbit.type === 'kepler' ? (
        <div className="fieldrow">
          <NumF path={[...base, 'orbit', 'a_m']} label="Semi-major axis" unit="km" scale={1000} />
          <NumF path={[...base, 'orbit', 'e']} label="Eccentricity" />
          <NumF path={[...base, 'orbit', 'inc_deg']} label="Inclination" unit="°" />
          <NumF path={[...base, 'orbit', 'raan_deg']} label="RAAN" unit="°" />
          <NumF path={[...base, 'orbit', 'argp_deg']} label="Argument of perigee" unit="°" />
          <NumF path={[...base, 'orbit', 'mean_anomaly_deg']} label="Mean anomaly" unit="°" />
          <TextF path={[...base, 'orbit', 'epoch']} label="Epoch (UTC, ISO 8601)" mono />
          <BoolF path={[...base, 'orbit', 'j2']} label="Secular J2" />
        </div>
      ) : null}
      {orbit.type === 'table' ? (
        <p className="ink2">
          Ephemeris table of {orbit.samples.length} ECEF samples, {orbit.samples[0]?.t} to {orbit.samples.at(-1)?.t}. Edit tables in YAML.
        </p>
      ) : null}
    </fieldset>
  );
}

function useContextUpdate() {
  return useContext(EditorContext).update;
}

function SatelliteEditor({ cfg, i }: { cfg: SystemConfig; i: number }) {
  const sat = cfg.satellites[i];
  const update = useContextUpdate();
  if (!sat) return <p className="ink2">No such satellite.</p>;
  const base: Path = ['satellites', i];
  const override = sat.ephemeris_sigma !== undefined && sat.ephemeris_sigma !== null;
  return (
    <div className="cfgform">
      <ContainerProblems path={base} />
      <fieldset>
        <legend>Satellite</legend>
        <div className="fieldrow">
          <TextF path={[...base, 'id']} label="Id" mono />
          <TextF path={[...base, 'name']} label="Name" />
          <NumF path={[...base, 'norad_id']} label="NORAD id" optional integer />
        </div>
        <TextF path={[...base, 'notes']} label="Notes" wide />
      </fieldset>
      <OrbitEditor base={base} sat={sat} />
      <fieldset>
        <legend>Ephemeris uncertainty</legend>
        <label>
          <input
            type="checkbox"
            checked={override}
            onChange={(e) => update([...base, 'ephemeris_sigma'], e.target.checked ? { pos_m: [100, 1000, 1000], vel_mps: [0.02, 0.02, 0.05] } : undefined)}
          />{' '}
          Override the {sat.orbit.type === 'tle' ? 'TLE' : 'operator'} default for this satellite
        </label>
        {override ? (
          <div className="fieldrow" style={{ gridTemplateColumns: '1fr 1fr' }}>
            <Vec3F path={[...base, 'ephemeris_sigma', 'pos_m']} label="Position 1-sigma" unit="m" />
            <Vec3F path={[...base, 'ephemeris_sigma', 'vel_mps']} label="Velocity 1-sigma" unit="m/s" />
          </div>
        ) : null}
      </fieldset>
      <fieldset>
        <legend>Transponders</legend>
        <ContainerProblems path={[...base, 'transponders']} />
        <div className="tablewrap">
          <table className="grid edittable">
            <thead>
              <tr>
                <th>Id</th>
                <th className="num">
                  Uplink center <span className="unit">MHz</span>
                </th>
                <th className="num">
                  Bandwidth <span className="unit">kHz</span>
                </th>
                <th className="num">
                  Translation <span className="unit">MHz</span>
                </th>
                <th className="num">
                  G/T <span className="unit">dB/K</span>
                </th>
                <th className="num">
                  SFD <span className="unit">dBW/m²</span>
                </th>
                <th className="num">
                  Sat. EIRP <span className="unit">dBW</span>
                </th>
                <th className="num">
                  Group delay <span className="unit">µs</span>
                </th>
                <th className="num">
                  Downlink <span className="unit">MHz</span>
                </th>
                <th />
              </tr>
            </thead>
            <tbody>
              {sat.transponders.map((tp, t) => {
                const p: Path = [...base, 'transponders', t];
                return (
                  <tr key={t}>
                    <td>
                      <TextF path={[...p, 'id']} label="" aria={`Transponder ${t + 1} id`} mono />
                      <ContainerProblems path={p} />
                    </td>
                    <td className="num">
                      <FreqF path={[...p, 'uplink_center_hz']} label="" aria={`Transponder ${tp.id} uplink center, MHz`} />
                    </td>
                    <td className="num">
                      <NumF path={[...p, 'bandwidth_hz']} label="" aria={`Transponder ${tp.id} bandwidth, kHz`} scale={1000} />
                    </td>
                    <td className="num">
                      <FreqF path={[...p, 'translation_hz']} label="" aria={`Transponder ${tp.id} translation, MHz`} />
                    </td>
                    <td className="num">
                      <NumF path={[...p, 'g_over_t_dbk']} label="" aria={`Transponder ${tp.id} G/T, dB/K`} />
                    </td>
                    <td className="num">
                      <NumF path={[...p, 'sfd_dbw_m2']} label="" aria={`Transponder ${tp.id} SFD, dBW/m²`} />
                    </td>
                    <td className="num">
                      <NumF path={[...p, 'eirp_sat_dbw']} label="" aria={`Transponder ${tp.id} saturated EIRP, dBW`} />
                    </td>
                    <td className="num">
                      <NumF path={[...p, 'group_delay_s']} label="" aria={`Transponder ${tp.id} group delay, µs`} scale={1e-6} optional />
                    </td>
                    <td className="num ink2">{typeof tp.uplink_center_hz === 'number' && typeof tp.translation_hz === 'number' ? mhz(tp.uplink_center_hz + tp.translation_hz, 4) : '—'}</td>
                    <td>
                      <button type="button" className="btn small" aria-label={`Remove transponder ${tp.id}`} onClick={() => update([...base, 'transponders'], sat.transponders.filter((_, k) => k !== t))}>
                        Remove
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <div>
          <button
            type="button"
            className="btn small"
            onClick={() => {
              // a new transponder copies this satellite's last one, else the last one configured anywhere (ADAPT-10)
              const last = sat.transponders.at(-1) ?? cfg.satellites.flatMap((x) => x.transponders).at(-1);
              update([...base, 'transponders'], [
                ...sat.transponders,
                { id: `tp-${sat.transponders.length + 1}`, uplink_center_hz: (last?.uplink_center_hz ?? 300e6) + (last?.bandwidth_hz ?? 25e3) * 2, bandwidth_hz: last?.bandwidth_hz ?? 25e3, translation_hz: last?.translation_hz ?? -41e6, g_over_t_dbk: last?.g_over_t_dbk ?? -14, sfd_dbw_m2: last?.sfd_dbw_m2 ?? -136, eirp_sat_dbw: last?.eirp_sat_dbw ?? 26 },
              ]);
            }}
          >
            Add transponder
          </button>
        </div>
      </fieldset>
    </div>
  );
}

// ------------------------------------------------------------------ sites and channels

function AntennaEditor({ base, ch, cfg }: { base: Path; ch: ReceiveChannel; cfg: SystemConfig }) {
  const update = useContextUpdate();
  const a = ch.antenna;
  const small = electricallySmall(ch, cfg.satellites);
  const setType = (t: string) => {
    const fresh: Record<string, unknown> = {
      isotropic: { type: 'isotropic', gain_dbi: 0 },
      parabolic: { type: 'parabolic', diameter_m: 1.2, efficiency: 0.55 },
      beam: { type: 'beam', gain_dbi: 10, hpbw_deg: 52, front_to_back_db: 20 },
    };
    update([...base, 'antenna'], fresh[t]);
  };
  return (
    <div className="field" style={{ gap: 6 }}>
      <span className="label">Antenna</span>
      <span className="seg" role="group" aria-label="Antenna type">
        {[
          ['parabolic', 'Parabolic'],
          ['beam', 'Beam (yagi, helix, patch)'],
          ['isotropic', 'Isotropic'],
        ].map(([t, l]) => (
          <button key={t} type="button" aria-pressed={a.type === t} onClick={() => a.type !== t && setType(t ?? '')}>
            {l}
          </button>
        ))}
        {a.type === 'table' ? <button type="button" aria-pressed>Pattern table</button> : null}
      </span>
      <ContainerProblems path={[...base, 'antenna']} hideWarnings={small !== null} />
      <div className="fieldrow">
        {a.type === 'parabolic' ? (
          <>
            <NumF path={[...base, 'antenna', 'diameter_m']} label="Diameter" unit="m" />
            <NumF path={[...base, 'antenna', 'efficiency']} label="Aperture efficiency" optional />
          </>
        ) : null}
        {a.type === 'beam' ? (
          <>
            <NumF path={[...base, 'antenna', 'gain_dbi']} label="Gain" unit="dBi" />
            <NumF path={[...base, 'antenna', 'hpbw_deg']} label="Half-power beamwidth" unit="°" />
            <NumF path={[...base, 'antenna', 'front_to_back_db']} label="Front to back" unit="dB" optional />
          </>
        ) : null}
        {a.type === 'isotropic' ? <NumF path={[...base, 'antenna', 'gain_dbi']} label="Gain" unit="dBi" optional /> : null}
        {a.type === 'table' ? <p className="ink2">Tabulated pattern of {a.theta_deg.length} points; edit in YAML.</p> : null}
      </div>
      {small ? (
        <div className="notice caution" role="note">
          {/* ledger: caution rung, the condition the brief names */}
          <strong>Electrically small dish.</strong> At {mhz(small.freqHz, 3)} MHz ({small.satId}) the wavelength is {fixed(small.wavelengthM, 2)} m, so this {fixed(small.diameterM, 2)} m
          dish is {fixed(small.diametersInWavelengths, 2)} wavelengths across, under 3. It behaves as a reflector, not a focusing dish: about {fixed(small.peakGainDbi, 1)} dBi
          with a beam about {fixed(small.hpbwDeg, 0)}° wide, so it also hears satellites well off its pointing and gives little protection from co-channel neighbors.
        </div>
      ) : null}
    </div>
  );
}

function PointingEditor({ base, ch, cfg }: { base: Path; ch: ReceiveChannel; cfg: SystemConfig }) {
  const update = useContextUpdate();
  const p = ch.pointing;
  const track = 'satellite_id' in p;
  const sats: Array<[string, string]> = cfg.satellites.map((s) => [s.id, `${s.id} (${s.name})`]);
  return (
    <div className="field" style={{ gap: 6 }}>
      <span className="label">Pointing</span>
      <span className="seg" role="group" aria-label="Pointing">
        <button type="button" aria-pressed={track} onClick={() => !track && update([...base, 'pointing'], { satellite_id: ('satellite_ids' in p ? p.satellite_ids[0] : undefined) ?? cfg.satellites[0]?.id ?? '' })}>
          Track a satellite
        </button>
        <button type="button" aria-pressed={!track} onClick={() => track && update([...base, 'pointing'], { az_deg: 180, el_deg: 30, satellite_ids: ['satellite_id' in p ? p.satellite_id : cfg.satellites[0]?.id ?? ''] })}>
          Fixed azimuth and elevation
        </button>
      </span>
      <ContainerProblems path={[...base, 'pointing']} />
      {track ? (
        <SelectF path={[...base, 'pointing', 'satellite_id']} label="Satellite" options={sats} />
      ) : (
        <div className="fieldrow">
          <NumF path={[...base, 'pointing', 'az_deg']} label="Azimuth" unit="° true" />
          <NumF path={[...base, 'pointing', 'el_deg']} label="Elevation" unit="°" />
          <div className="field">
            <span className="label">Watched satellites</span>
            {cfg.satellites.map((s) => {
              const ids = 'satellite_ids' in p ? p.satellite_ids : [];
              return (
                <label key={s.id}>
                  <input
                    type="checkbox"
                    checked={ids.includes(s.id)}
                    onChange={(e) => update([...base, 'pointing', 'satellite_ids'], e.target.checked ? [...ids, s.id] : ids.filter((x) => x !== s.id))}
                  />{' '}
                  <span className="mono">{s.id}</span>
                </label>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

function MonitorsEditor({ base, ch, cfg }: { base: Path; ch: ReceiveChannel; cfg: SystemConfig }) {
  const update = useContextUpdate();
  const { monitors, derived } = monitorsOf(ch, cfg.satellites);
  return (
    <div className="field" style={{ gap: 6 }}>
      <span className="label">
        Spectrum windows {derived ? '(derived: one per watched transponder)' : '(configured)'}
        <Info label="spectrum windows">
          Without configured windows the server derives one per transponder of each watched satellite, centered on its downlink, 1.25 bandwidths wide, at 2 frames per
          second. Configured windows do not follow later changes to the frequency plan.
        </Info>
      </span>
      <ContainerProblems path={[...base, 'monitors']} deep />
      <div className="tablewrap">
        <table className="grid edittable">
          <thead>
            <tr>
              <th>Id</th>
              <th className="num">
                Center <span className="unit">MHz</span>
              </th>
              <th className="num">
                Span <span className="unit">kHz</span>
              </th>
              <th className="num">nfft</th>
              <th className="num">navg</th>
              <th className="num">
                Rate <span className="unit">frames/s</span>
              </th>
              {!derived ? <th /> : null}
            </tr>
          </thead>
          <tbody>
            {derived
              ? monitors.map((m) => (
                  <tr key={m.id} className="muted">
                    <td className="mono">{m.id}</td>
                    <td className="num">{mhz(m.center_hz)}</td>
                    <td className="num">{khz(m.span_hz, 2)}</td>
                    <td className="num">{m.nfft}</td>
                    <td className="num">{m.navg}</td>
                    <td className="num">{m.frame_rate_hz}</td>
                  </tr>
                ))
              : monitors.map((_m, k) => {
                  const p: Path = [...base, 'monitors', k];
                  return (
                    <tr key={k}>
                      <td>
                        <TextF path={[...p, 'id']} label="" aria={`Window ${k + 1} id`} mono />
                      </td>
                      <td className="num">
                        <FreqF path={[...p, 'center_hz']} label="" aria={`Window ${k + 1} center, MHz`} />
                      </td>
                      <td className="num">
                        <NumF path={[...p, 'span_hz']} label="" aria={`Window ${k + 1} span, kHz`} scale={1000} />
                      </td>
                      <td className="num">
                        <NumF path={[...p, 'nfft']} label="" aria={`Window ${k + 1} nfft`} integer optional />
                      </td>
                      <td className="num">
                        <NumF path={[...p, 'navg']} label="" aria={`Window ${k + 1} navg`} integer optional />
                      </td>
                      <td className="num">
                        <NumF path={[...p, 'frame_rate_hz']} label="" aria={`Window ${k + 1} frame rate, frames/s`} optional />
                      </td>
                      <td>
                        <button type="button" className="btn small" onClick={() => update([...base, 'monitors'], (ch.monitors ?? []).filter((_, j) => j !== k))}>
                          Remove
                        </button>
                      </td>
                    </tr>
                  );
                })}
          </tbody>
        </table>
      </div>
      <div style={{ display: 'flex', gap: 8 }}>
        {derived ? (
          <button type="button" className="btn small" onClick={() => update([...base, 'monitors'], monitors)}>
            Configure windows explicitly
          </button>
        ) : (
          <>
            <button type="button" className="btn small" onClick={() => update([...base, 'monitors'], [...(ch.monitors ?? []), { ...(ch.monitors?.at(-1) ?? { center_hz: 260e6, span_hz: 31250 }), id: `window-${(ch.monitors?.length ?? 0) + 1}` }])}>
              Add window
            </button>
            <button type="button" className="btn small" onClick={() => update([...base, 'monitors'], [])}>
              Return to derived windows
            </button>
          </>
        )}
      </div>
    </div>
  );
}

function SiteEditor({ cfg, i }: { cfg: SystemConfig; i: number }) {
  const site = cfg.sites[i];
  const update = useContextUpdate();
  if (!site) return <p className="ink2">No such site.</p>;
  const base: Path = ['sites', i];
  const lat = typeof site.lat_deg === 'number' ? site.lat_deg : NaN;
  const lon = typeof site.lon_deg === 'number' ? site.lon_deg : NaN;
  return (
    <div className="cfgform">
      <ContainerProblems path={base} />
      <fieldset>
        <legend>Site</legend>
        <div className="fieldrow">
          <TextF path={[...base, 'id']} label="Id" mono />
          <TextF path={[...base, 'name']} label="Name" />
          <NumF path={[...base, 'lat_deg']} label="Latitude" unit="° (N positive)" />
          <NumF path={[...base, 'lon_deg']} label="Longitude" unit="° (E positive)" />
          <NumF path={[...base, 'alt_m']} label="Altitude" unit="m above WGS 84" optional />
          <NumF path={[...base, 'antenna_height_m']} label="Antenna height" unit="m AGL" optional />
          <NumF path={[...base, 'clock', 'sigma_ns']} label="Clock 1-sigma" unit="ns" optional />
          <TextF
            path={[...base, 'time_zone']}
            label="Time zone (IANA)"
            mono
            optional
            hint={site.time_zone ? (knownZone(site.time_zone) ? 'shown in the clock strip' : 'this browser does not know this zone') : 'empty: inferred from the position'}
          />
        </div>
        {Number.isFinite(lat) && Number.isFinite(lon) && Math.abs(lat) <= 90 && Math.abs(lon) <= 180 ? (
          <p className="mono ink2">
            {formatLatLon(lat, lon, 4)} WGS 84, MGRS {formatMgrs(lat, lon) ?? 'not defined (UPS zone)'}
          </p>
        ) : null}
      </fieldset>
      {site.channels.map((ch, c) => {
        const p: Path = [...base, 'channels', c];
        return (
          <fieldset key={c}>
            <legend>
              Channel <span className="mono">{ch.id}</span>
            </legend>
            <ContainerProblems path={p} />
            <div className="fieldrow">
              <TextF path={[...p, 'id']} label="Id" mono />
              <TextF path={[...p, 'name']} label="Name" optional />
              <NumF path={[...p, 'system_noise_temp_k']} label="System noise temperature" unit="K" optional />
              <NumF path={[...p, 'polarization_loss_db']} label="Polarization loss" unit="dB" optional />
              <NumF path={[...p, 'max_sample_rate_hz']} label="Max sample rate" unit="kHz" scale={1000} optional />
            </div>
            <AntennaEditor base={p} ch={ch} cfg={cfg} />
            <PointingEditor base={p} ch={ch} cfg={cfg} />
            <MonitorsEditor base={p} ch={ch} cfg={cfg} />
            <div>
              <button type="button" className="btn small danger" onClick={() => update([...base, 'channels'], site.channels.filter((_, k) => k !== c))}>
                Remove channel {ch.id}
              </button>
            </div>
          </fieldset>
        );
      })}
      <div>
        <button
          type="button"
          className="btn small"
          onClick={() =>
            update([...base, 'channels'], [
              ...site.channels,
              { id: `${site.id}-ch${site.channels.length + 1}`, antenna: { type: 'parabolic', diameter_m: 1.2, efficiency: 0.55 }, pointing: { satellite_id: cfg.satellites[0]?.id ?? '' } },
            ])
          }
        >
          Add channel
        </button>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ carriers, processing, general

function CarriersEditor({ cfg }: { cfg: SystemConfig }) {
  const update = useContextUpdate();
  const carriers = cfg.carriers ?? [];
  const sats: Array<[string, string]> = cfg.satellites.map((s) => [s.id, s.id]);
  return (
    <div className="cfgform">
      {carriers.map((car, k) => {
        const p: Path = ['carriers', k];
        const tps: Array<[string, string]> = (cfg.satellites.find((s) => s.id === car.satellite_id)?.transponders ?? []).map((t) => [t.id, `${t.id} (${mhz(t.uplink_center_hz, 4)} MHz up)`]);
        return (
          <fieldset key={k}>
            <legend>
              Carrier <span className="mono">{car.id}</span> {car.use_as_reference ? '(reference emitter)' : ''}
            </legend>
            <ContainerProblems path={p} />
            <div className="fieldrow">
              <TextF path={[...p, 'id']} label="Id" mono />
              <TextF path={[...p, 'name']} label="Name" />
              <SelectF path={[...p, 'satellite_id']} label="Satellite" options={sats} />
              <SelectF path={[...p, 'transponder_id']} label="Transponder" options={tps} />
              <FreqF path={[...p, 'uplink_center_hz']} label="Uplink center" />
              <NumF path={[...p, 'bandwidth_hz']} label="Bandwidth" unit="kHz" scale={1000} />
            </div>
            <BoolF path={[...p, 'use_as_reference']} label="Use as a calibration reference (needs a surveyed location and an emitter model)" />
            <label>
              <input type="checkbox" checked={!!car.location} onChange={(e) => update([...p, 'location'], e.target.checked ? { lat_deg: 0, lon_deg: 0, alt_m: 0, antenna_height_m: 10 } : undefined)} /> Surveyed location known
            </label>
            {car.location ? (
              <div className="fieldrow">
                <NumF path={[...p, 'location', 'lat_deg']} label="Latitude" unit="°" />
                <NumF path={[...p, 'location', 'lon_deg']} label="Longitude" unit="°" />
                <NumF path={[...p, 'location', 'alt_m']} label="Altitude" unit="m" optional />
                <NumF path={[...p, 'location', 'antenna_height_m']} label="Antenna height" unit="m" optional />
              </div>
            ) : null}
            <label>
              <input type="checkbox" checked={!!car.emitter} onChange={(e) => update([...p, 'emitter'], e.target.checked ? { eirp_dbw: 13, bandwidth_hz: car.bandwidth_hz, waveform: 'noise' } : undefined)} /> Emitter model known
            </label>
            {car.emitter ? (
              <div className="fieldrow">
                <NumF path={[...p, 'emitter', 'eirp_dbw']} label="EIRP" unit="dBW" />
                <NumF path={[...p, 'emitter', 'bandwidth_hz']} label="Occupied bandwidth" unit="kHz" scale={1000} />
                <SelectF path={[...p, 'emitter', 'waveform']} label="Waveform" options={[['noise', 'noise'], ['psk', 'psk'], ['chirp', 'chirp'], ['cw', 'cw']]} />
              </div>
            ) : null}
            <div>
              <button type="button" className="btn small danger" onClick={() => update(['carriers'], carriers.filter((_, j) => j !== k))}>
                Remove carrier {car.id}
              </button>
            </div>
          </fieldset>
        );
      })}
      <div>
        <button
          type="button"
          className="btn small"
          onClick={() => {
            const sat = cfg.satellites[0];
            const tp = sat?.transponders[0];
            update(['carriers'], [...carriers, { id: `carrier-${carriers.length + 1}`, name: 'New carrier', satellite_id: sat?.id ?? '', transponder_id: tp?.id ?? '', uplink_center_hz: tp?.uplink_center_hz ?? 300e6, bandwidth_hz: 5000 }]);
          }}
        >
          Add carrier
        </button>
      </div>
    </div>
  );
}

function ProcessingEditor() {
  const P = (...p: Array<string | number>): Path => ['processing', ...p];
  return (
    <div className="cfgform">
      <fieldset>
        <legend>Detection and verdict</legend>
        <ContainerProblems path={P('detection')} />
        <div className="fieldrow">
          <NumF path={P('detection', 'threshold_db')} label="Threshold" unit="dB" optional />
          <NumF path={P('detection', 'confirm_m')} label="Confirm M" integer optional />
          <NumF path={P('detection', 'confirm_n')} label="of N frames" integer optional />
          <NumF path={P('detection', 'release_frames')} label="Release after" unit="frames" integer optional />
          <NumF path={P('detection', 'merge_bins')} label="Merge gaps up to" unit="bins" integer optional />
          <NumF path={P('detection', 'baseline_tau_s')} label="Baseline span" unit="s" optional />
          <NumF path={P('detection', 'power_anomaly_db')} label="Carrier anomaly" unit="dB" optional />
          <NumF path={P('detection', 'min_site_separation_km')} label="Uplink site separation" unit="km" optional />
          <NumF path={P('detection', 'verdict_settle_s')} label="Verdict settle time" unit="s" optional />
          <NumF path={P('detection', 'sensitivity_margin_db')} label="Capability margin" unit="dB" optional />
        </div>
      </fieldset>
      <fieldset>
        <legend>Capture</legend>
        <div className="fieldrow">
          <NumF path={P('capture', 'duration_s')} label="Duration" unit="s" optional />
          <NumF path={P('capture', 'lead_s')} label="Lead" unit="s" optional />
          <NumF path={P('capture', 'interval_s')} label="Interval" unit="s" optional />
          <NumF path={P('capture', 'oversample')} label="Oversample" optional />
          <NumF path={P('capture', 'min_sample_rate_hz')} label="Min sample rate" unit="kHz" scale={1000} optional />
          <NumF path={P('capture', 'max_samples')} label="Max samples" integer optional />
          <NumF path={P('capture', 'timeout_s')} label="Timeout" unit="s" optional />
        </div>
      </fieldset>
      <fieldset>
        <legend>Correlation</legend>
        <div className="fieldrow">
          <NumF path={P('caf', 'min_snr_db')} label="Detection SNR" unit="dB" optional />
          <NumF path={P('caf', 'max_tdoa_sigma_s')} label="Max TDOA sigma" unit="µs" scale={1e-6} optional />
          <NumF path={P('caf', 'doppler_margin_hz')} label="Doppler margin" unit="Hz" optional />
          <NumF path={P('caf', 'delay_margin_s')} label="Delay margin" unit="µs" scale={1e-6} optional />
        </div>
      </fieldset>
      <fieldset>
        <legend>Solver</legend>
        <div className="fieldrow">
          <NumF path={P('solver', 'height_m')} label="Emitter height" unit="m" optional />
          <NumF path={P('solver', 'min_elevation_deg')} label="Elevation mask" unit="°" optional />
          <NumF path={P('solver', 'confidence')} label="Ellipse confidence" optional />
          <NumF path={P('solver', 'grid_step_deg')} label="Search grid step" unit="°" optional />
          <NumF path={P('solver', 'n_starts')} label="Starts" integer optional />
          <NumF path={P('solver', 'chi2_ambiguity')} label="Ambiguity Δχ²" optional />
          <NumF path={P('solver', 'max_measurements')} label="Max measurements" integer optional />
        </div>
      </fieldset>
      <fieldset>
        <legend>Uncertainty (1-sigma systematics)</legend>
        <div className="fieldrow" style={{ gridTemplateColumns: '1fr 1fr' }}>
          <Vec3F path={P('uncertainty', 'ephemeris', 'operator', 'pos_m')} label="Operator ephemeris position" unit="m" />
          <Vec3F path={P('uncertainty', 'ephemeris', 'operator', 'vel_mps')} label="Operator ephemeris velocity" unit="m/s" />
          <Vec3F path={P('uncertainty', 'ephemeris', 'tle', 'pos_m')} label="TLE position" unit="m" />
          <Vec3F path={P('uncertainty', 'ephemeris', 'tle', 'vel_mps')} label="TLE velocity" unit="m/s" />
        </div>
        <div className="fieldrow">
          <NumF path={P('uncertainty', 'sat_osc_frac')} label="Satellite oscillator" unit="fraction" optional />
          <NumF path={P('uncertainty', 'site_osc_frac')} label="Site oscillator" unit="fraction" optional />
          <NumF path={P('uncertainty', 'tdoa_bias_ns')} label="TDOA bias" unit="ns" optional />
        </div>
      </fieldset>
      <fieldset>
        <legend>Calibration</legend>
        <div className="fieldrow">
          <BoolF path={P('calibration', 'enabled')} label="Calibrate with reference emitters" />
          <NumF path={P('calibration', 'interval_s')} label="Interval" unit="s" optional />
          <NumF path={P('calibration', 'max_age_s')} label="Valid for" unit="s" optional />
        </div>
      </fieldset>
      <fieldset>
        <legend>Region of interest</legend>
        <ContainerProblems path={P('region')} />
        <div className="fieldrow">
          <NumF path={P('region', 'lat_min')} label="South edge" unit="° N" optional />
          <NumF path={P('region', 'lat_max')} label="North edge" unit="° N" optional />
          <NumF path={P('region', 'lon_min')} label="West edge" unit="° E" optional info={<Info label="region longitudes">The region runs east from the west edge to the east edge; an east edge smaller than the west edge crosses the antimeridian. -180 to 180 is the whole globe.</Info>} />
          <NumF path={P('region', 'lon_max')} label="East edge" unit="° E" optional />
        </div>
      </fieldset>
    </div>
  );
}

function GeneralEditor({ version }: { version: number }) {
  return (
    <div className="cfgform">
      <fieldset>
        <legend>Configuration</legend>
        <div className="fieldrow">
          <TextF path={['name']} label="Name" mono />
          <NumF path={['dut1_s']} label="UT1 − UTC" unit="s" optional />
          <div className="field">
            <span className="label">Version on the server</span>
            <span className="mono">{version}</span>
          </div>
        </div>
        <TextF path={['description']} label="Description" wide optional />
      </fieldset>
    </div>
  );
}

function YamlPanel({ draft, onImport, fileBase }: { draft: SystemConfig; onImport: (c: SystemConfig) => void; fileBase: string }) {
  const [text, setText] = useState('');
  const [error, setError] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const yaml = useMemo(() => toYaml(draft, { lineWidth: 0 }), [draft]);
  const load = (t: string) => {
    try {
      const v = parseYaml(t) as unknown;
      if (!v || typeof v !== 'object' || Array.isArray(v)) throw new Error('the document is not a mapping');
      const obj = v as Record<string, unknown>;
      const cfg = (obj.config && typeof obj.config === 'object' ? obj.config : obj) as SystemConfig;
      setError(null);
      onImport(cfg);
    } catch (e) {
      setError(`Not imported: ${(e as Error).message}`);
    }
  };
  return (
    <div className="cfgform">
      <fieldset>
        <legend>Export</legend>
        <div style={{ display: 'flex', gap: 8 }}>
          <button
            type="button"
            className="btn"
            onClick={() => {
              const blob = new Blob([`# exported ${new Date().toISOString()}\n${yaml}`], { type: 'application/yaml' });
              const a = document.createElement('a');
              a.href = URL.createObjectURL(blob);
              a.download = `${fileBase}.yaml`;
              a.click();
              setTimeout(() => URL.revokeObjectURL(a.href), 1000);
            }}
          >
            Download YAML
          </button>
          <button type="button" className="btn" onClick={() => void navigator.clipboard?.writeText(yaml)}>
            Copy YAML
          </button>
        </div>
      </fieldset>
      <fieldset>
        <legend>Import</legend>
        <p>An imported file replaces the draft; nothing is saved until you save.</p>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <input ref={fileRef} type="file" accept=".yaml,.yml,.json" aria-label="YAML file to import" onChange={async (e) => e.target.files?.[0] && load(await e.target.files[0].text())} />
        </div>
        <label htmlFor="yaml-paste">Or paste YAML</label>
        <textarea id="yaml-paste" className="yamlbox" value={text} onChange={(e) => setText(e.target.value)} />
        <div>
          <button type="button" className="btn" disabled={!text.trim()} onClick={() => load(text)}>
            Import pasted YAML
          </button>
        </div>
        {error ? <p className="err-text" role="alert">{error}</p> : null}
      </fieldset>
      <fieldset>
        <legend>Draft as YAML</legend>
        <pre className="mono" style={{ fontSize: 12, maxHeight: 400, overflow: 'auto', margin: 0, padding: 8, background: 'var(--surface)', border: '1px solid var(--rule)' }}>{yaml}</pre>
      </fieldset>
    </div>
  );
}

// ------------------------------------------------------------------ the screen

/**
 * The configuration as it is sent back: channels whose windows the server derived carry
 * `monitors: []` so they keep following the frequency plan (ConfigEnvelope.derived_monitors).
 * The editor still shows their windows (derived again on the client by the same rule).
 */
function sendForm(cfg: SystemConfig, derived: string[] | undefined): SystemConfig {
  if (!derived?.length) return cfg;
  const set = new Set(derived);
  return { ...cfg, sites: cfg.sites.map((s) => ({ ...s, channels: s.channels.map((c) => (set.has(c.id) ? { ...c, monitors: [] } : c)) })) };
}

interface Conflict {
  theirVersion: number;
  theirConfig: SystemConfig;
  theirEtag: string | null;
  changed: string[];
}

export function ConfigureView() {
  const res = configResource.use();
  const route = useRoute();
  const section = parseSection(route.id);
  const serverVersion = useLive((s) => s.configVersion);
  const [draft, setDraft] = useState<SystemConfig | null>(null);
  const [base, setBase] = useState<{ config: SystemConfig; version: number; etag: string } | null>(null);
  const [errors, setErrors] = useState<FieldProblem[]>([]);
  const [warnings, setWarnings] = useState<FieldProblem[]>([]);
  const [validating, setValidating] = useState(false);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<{ kind: 'ok' | 'error'; text: string } | null>(null);
  const [conflict, setConflict] = useState<Conflict | null>(null);
  const [removed, setRemoved] = useState<{ what: string; doc: SystemConfig } | null>(null);

  const dirty = useMemo(() => !!draft && !!base && JSON.stringify(draft) !== JSON.stringify(base.config), [draft, base]);

  // start editing from the server's configuration, and follow newer versions saved
  // elsewhere while there are no unsaved changes here
  useEffect(() => {
    if (!res.data) return;
    const env = res.data.envelope;
    const newer = base !== null && env.version > base.version && !dirty && !conflict;
    if (base && !newer) return;
    const cfg = sendForm(env.config, env.derived_monitors);
    setBase({ config: cfg, version: env.version, etag: res.data.etag ?? `"${env.version}"` });
    setDraft(structuredClone(cfg));
    setWarnings(env.warnings ?? []);
    if (newer) {
      setErrors([]);
      setMessage({ kind: 'ok', text: `Version ${env.version} was saved elsewhere and is now shown` });
    }
  }, [res.data, base, dirty, conflict]);
  const changedCount = useMemo(() => (draft && base ? diffPaths(base.config, draft).length : 0), [draft, base]);

  // validate on the server while editing (debounced)
  const seq = useRef(0);
  useEffect(() => {
    if (!draft || !dirty) return;
    const my = ++seq.current;
    const t = setTimeout(async () => {
      setValidating(true);
      try {
        const r = await api.validateConfig(draft);
        if (my === seq.current) {
          setErrors(r.errors);
          setWarnings(r.warnings);
        }
      } catch (e) {
        if (my === seq.current) setMessage({ kind: 'error', text: `Could not validate: ${(e as Error).message}` });
      } finally {
        if (my === seq.current) setValidating(false);
      }
    }, 500);
    return () => clearTimeout(t);
  }, [draft, dirty]);

  const update = (path: Path, value: unknown) => {
    setDraft((d) => (d ? setIn(d, path, value) : d));
    setMessage(null);
  };

  const save = async (etagOverride?: string) => {
    if (!draft || !base) return;
    setSaving(true);
    setMessage(null);
    try {
      const r = await api.putConfig(draft, etagOverride ?? base.etag);
      const env = r.data;
      configResource.set({ envelope: env, etag: r.etag });
      const cfg = sendForm(env.config, env.derived_monitors);
      setBase({ config: cfg, version: env.version, etag: r.etag ?? `"${env.version}"` });
      setDraft(structuredClone(cfg));
      setErrors([]);
      setWarnings(env.warnings ?? []);
      setConflict(null);
      setMessage({ kind: 'ok', text: `Saved as version ${env.version} at ${dtg(new Date())}` });
    } catch (e) {
      if (e instanceof ConflictError) {
        try {
          const latest = await api.getConfig();
          const theirs = sendForm(latest.data.config, latest.data.derived_monitors);
          setConflict({ theirVersion: latest.data.version, theirConfig: theirs, theirEtag: latest.etag, changed: diffPaths(base.config, theirs).filter((p) => p !== 'version') });
        } catch (e2) {
          setMessage({ kind: 'error', text: `Version conflict, and the current version could not be read: ${(e2 as Error).message}` });
        }
      } else if (e instanceof ValidationError) {
        setErrors(e.errors);
        setWarnings(e.warnings);
        setMessage({ kind: 'error', text: `Not saved: ${e.errors.length} problem${e.errors.length === 1 ? '' : 's'} to fix (marked below).` });
      } else {
        setMessage({ kind: 'error', text: `Not saved: ${(e as Error).message}` });
      }
    } finally {
      setSaving(false);
    }
  };

  const discard = () => {
    if (!base) return;
    setDraft(structuredClone(base.config));
    setErrors([]);
    setMessage(null);
  };

  if (res.error && !res.data) return <ErrorNote error={`Could not load the configuration: ${res.error}`} retry={res.reload} />;
  if (!draft || !base) return <Loading what="the configuration" />;

  const errBySection = new Map<string, number>();
  for (const e of errors) errBySection.set(sectionOf(e.loc), (errBySection.get(sectionOf(e.loc)) ?? 0) + 1);
  const go = (s: Section) => navigate(href('configure', sectionId(s)));
  const NavBtn = ({ s, label, count }: { s: Section; label: ReactNode; count?: number }) => {
    const id = sectionId(s);
    const bad = errBySection.get(id) ?? 0;
    return (
      <button type="button" aria-current={sectionId(section) === id ? 'true' : undefined} onClick={() => go(s)}>
        <span>{label}</span>
        <span>
          {bad ? <span className="bad">{bad} problem{bad > 1 ? 's' : ''}</span> : count !== undefined ? <span className="count">{count}</span> : null}
        </span>
      </button>
    );
  };
  const removeSat = (i: number) => {
    setRemoved({ what: draft.satellites[i]?.id ?? 'satellite', doc: draft });
    update(['satellites'], draft.satellites.filter((_, k) => k !== i));
    go({ kind: 'general' });
  };
  const removeSite = (i: number) => {
    setRemoved({ what: draft.sites[i]?.id ?? 'site', doc: draft });
    update(['sites'], draft.sites.filter((_, k) => k !== i));
    go({ kind: 'general' });
  };
  const jumpTo = (loc: string) => {
    go(parseSection(sectionOf(loc)));
    setTimeout(() => {
      const el = document.getElementById(`cfg-${loc}`) ?? document.getElementById(`cfg-${loc.split('.').slice(0, -1).join('.')}`);
      el?.scrollIntoView({ block: 'center' });
      el?.focus();
    }, 60);
  };
  const serverAhead = serverVersion !== null && serverVersion > base.version && !conflict;

  return (
    <EditorContext.Provider value={{ doc: draft, update, errors, warnings }}>
      <div className="config">
        <nav className="cfgnav" aria-label="Configuration sections">
          <NavBtn s={{ kind: 'general' }} label="General" />
          <h3>Satellites</h3>
          {draft.satellites.map((s, i) => (
            <NavBtn key={i} s={{ kind: 'sat', i }} label={<span className="mono">{s.id || '(no id)'}</span>} count={s.transponders.length} />
          ))}
          <button
            type="button"
            onClick={() => {
              update(['satellites'], [...draft.satellites, { id: `sat-${draft.satellites.length + 1}`, name: 'New satellite', orbit: { type: 'geo', lon_deg: 0, inclination_deg: 0, epoch: '2026-01-01T00:00:00Z' }, transponders: [] }]);
              go({ kind: 'sat', i: draft.satellites.length });
            }}
          >
            <span className="ink2">Add satellite</span>
          </button>
          <h3>Sites</h3>
          {draft.sites.map((s, i) => (
            <NavBtn key={i} s={{ kind: 'site', i }} label={<span className="mono">{s.id || '(no id)'}</span>} count={s.channels.length} />
          ))}
          <button
            type="button"
            onClick={() => {
              update(['sites'], [...draft.sites, { id: `site-${draft.sites.length + 1}`, name: 'New site', lat_deg: 0, lon_deg: 0, channels: [] }]);
              go({ kind: 'site', i: draft.sites.length });
            }}
          >
            <span className="ink2">Add site</span>
          </button>
          <h3>Plan and processing</h3>
          <NavBtn s={{ kind: 'carriers' }} label="Carriers and references" count={draft.carriers?.length ?? 0} />
          <NavBtn s={{ kind: 'processing' }} label="Processing" />
          <NavBtn s={{ kind: 'yaml' }} label="YAML import and export" />
        </nav>
        <div className="cfgmain">
          <div className="toolbar" role="region" aria-label="Save">
            <strong>
              Version {base.version}
              {dirty ? `, ${changedCount} unsaved change${changedCount === 1 ? '' : 's'}` : ', no changes'}
            </strong>
            <span className="ink2" aria-live="polite">
              {validating ? 'checking on the server…' : dirty ? (errors.length ? `${errors.length} problem${errors.length === 1 ? '' : 's'}` : 'checked: no problems') : ''}
            </span>
            <span className="spacer" />
            {message ? (
              <span className={message.kind === 'error' ? 'err-text' : 'ink2'} role={message.kind === 'error' ? 'alert' : 'status'}>
                {message.text}
              </span>
            ) : null}
            <button type="button" className="btn" disabled={!dirty || saving} onClick={discard}>
              Discard changes
            </button>
            <button type="button" className="btn primary" disabled={!dirty || saving} onClick={() => void save()}>
              {saving ? 'Saving…' : `Save as version ${base.version + 1}`}
            </button>
          </div>
          {removed ? (
            <div className="notice">
              Removed <span className="mono">{removed.what}</span> from the draft.{' '}
              <button
                type="button"
                className="linkbtn"
                onClick={() => {
                  setDraft(removed.doc);
                  setRemoved(null);
                }}
              >
                Undo
              </button>
            </div>
          ) : null}
          {serverAhead ? <div className="notice caution">Version {serverVersion} is now on the server. Saving will ask how to combine it with your changes.</div> : null}
          {conflict ? (
            <div className="notice serious" role="alert">
              <p>
                <strong>
                  Version {conflict.theirVersion} was saved while you edited version {base.version}.
                </strong>{' '}
                It differs from version {base.version} in {conflict.changed.length} place{conflict.changed.length === 1 ? '' : 's'}
                {conflict.changed.length ? `: ${conflict.changed.slice(0, 6).join(', ')}${conflict.changed.length > 6 ? ', …' : ''}` : ''}. Your draft has {changedCount} change
                {changedCount === 1 ? '' : 's'}.
              </p>
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                <button
                  type="button"
                  className="btn"
                  onClick={() => {
                    configResource.set({ envelope: { version: conflict.theirVersion, config: conflict.theirConfig, warnings: [] }, etag: conflict.theirEtag });
                    setBase({ config: conflict.theirConfig, version: conflict.theirVersion, etag: conflict.theirEtag ?? `"${conflict.theirVersion}"` });
                    setDraft(structuredClone(conflict.theirConfig));
                    setConflict(null);
                    setErrors([]);
                    setMessage({ kind: 'ok', text: `Loaded version ${conflict.theirVersion}; your changes were discarded` });
                  }}
                >
                  Load version {conflict.theirVersion}, discard mine
                </button>
                <button type="button" className="btn primary" onClick={() => void save(conflict.theirEtag ?? `"${conflict.theirVersion}"`)}>
                  Save mine over version {conflict.theirVersion}
                </button>
                <button type="button" className="btn" onClick={() => setConflict(null)}>
                  Keep editing
                </button>
              </div>
            </div>
          ) : null}
          {errors.length ? (
            <ul className="problems" aria-label="Problems">
              {errors.slice(0, 12).map((e, i) => (
                <li key={i}>
                  <button type="button" className="linkbtn mono" onClick={() => jumpTo(e.loc)}>
                    {e.loc}
                  </button>
                  : <span className="err-text">{e.msg}</span>
                </li>
              ))}
              {errors.length > 12 ? <li>and {errors.length - 12} more</li> : null}
            </ul>
          ) : null}
          {section.kind === 'general' ? (
            <>
              <GeneralEditor version={base.version} />
              {warnings.length ? (
                <div className="cfgform">
                  <fieldset>
                    <legend>Warnings</legend>
                    {warnings.map((w, i) => (
                      <div key={i} className="notice caution">
                        <button type="button" className="linkbtn mono" onClick={() => jumpTo(w.loc)}>
                          {w.loc}
                        </button>
                        : {w.msg}
                      </div>
                    ))}
                  </fieldset>
                </div>
              ) : null}
            </>
          ) : null}
          {section.kind === 'sat' ? (
            <>
              <SatelliteEditor cfg={draft} i={section.i} />
              <div className="cfgform">
                <div>
                  <button type="button" className="btn danger" onClick={() => removeSat(section.i)}>
                    Remove satellite {draft.satellites[section.i]?.id}
                  </button>
                </div>
              </div>
            </>
          ) : null}
          {section.kind === 'site' ? (
            <>
              <SiteEditor cfg={draft} i={section.i} />
              <div className="cfgform">
                <div>
                  <button type="button" className="btn danger" onClick={() => removeSite(section.i)}>
                    Remove site {draft.sites[section.i]?.id}
                  </button>
                </div>
              </div>
            </>
          ) : null}
          {section.kind === 'carriers' ? <CarriersEditor cfg={draft} /> : null}
          {section.kind === 'processing' ? <ProcessingEditor /> : null}
          {section.kind === 'yaml' ? (
            <YamlPanel
              draft={draft}
              fileBase={`${draft.name || 'config'}-v${base.version}${dirty ? '-draft' : ''}-${isoFileStamp(new Date())}`}
              onImport={(c) => {
                setDraft(c);
                setMessage({ kind: 'ok', text: 'Imported into the draft; check the problems list, then save' });
              }}
            />
          ) : null}
        </div>
      </div>
    </EditorContext.Provider>
  );
}

