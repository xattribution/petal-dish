// Network health per site: connection, latency, clock source, lock and estimated offset,
// driver, software, channels and the age of each channel's last spectrum frame.
import { Fragment, useMemo } from 'react';
import type { NodeStatus } from '../api/types.gen';
import { Empty, Info, Loading, Status, StatusSymbol, useTick } from '../components/common';
import { nodeLevel } from '../domain/status';
import { formatLatLon, formatMgrs } from '../format/geo';
import { clockOffset, fixed, plural } from '../format/units';
import { useRoute } from '../router';
import { configResource } from '../state/resources';
import { useLive } from '../state/store';
import { dtg, formatAge } from '../time/dtg';

/** How long an offline node has been silent, from its channels' newest frame. */
function offlineFor(n: NodeStatus): string {
  const ages = n.channels.map((c) => c.last_frame_age_s).filter((a): a is number => a !== null && a !== undefined);
  return ages.length ? `no frames for ${formatAge(Math.min(...ages))}` : 'not connected since the server started';
}

export function NetworkView() {
  const status = useLive((s) => s.status);
  const link = useLive((s) => s.link);
  const cfg = configResource.use().data?.envelope.config ?? null;
  const route = useRoute();
  const focus = route.params.get('site');
  const now = useTick(1000);
  const age = status ? (now - status.receivedAt) / 1000 : Infinity;
  const sites = useMemo(() => new Map((cfg?.sites ?? []).map((s) => [s.id, s])), [cfg]);

  if (!status) {
    return link.kind === 'reconnecting' ? (
      <Empty title="No status from the server">The console is not connected ({link.reason}). The table appears with the first status report.</Empty>
    ) : (
      <Loading what="network status" />
    );
  }
  const stale = age > 3;
  return (
    <div className="view">
      <div className="toolbar">
        <h2 style={{ margin: 0 }}>Network</h2>
        <span className="ink2">
          {status.nodes.filter((n) => n.online).length} of {status.nodes.length} sites online, configuration version {status.config_version}
        </span>
        <span className="spacer" />
        {stale ? (
          <span className="status">
            <StatusSymbol level="caution" />
            <span>Stale: last status {formatAge(age)} ago; values below are as of {dtg(new Date(status.receivedAt))}</span>
          </span>
        ) : (
          <span className="ink2">Updated every second</span>
        )}
      </div>
      <div className="tablewrap" style={{ opacity: stale ? 0.75 : 1 }}>
        <table className="grid" aria-label="Sites and their nodes">
          <thead>
            <tr>
              <th>
                Site <span className="unit">WGS 84, MGRS</span>
              </th>
              <th>Connection</th>
              <th className="num">
                Latency <span className="unit">ms</span>
              </th>
              <th>Clock</th>
              <th className="num">
                Est. offset{' '}
                <Info label="estimated clock offset">The node's estimate of true time minus labeled time. Displayed only; it is never applied to measurements.</Info>
              </th>
              <th>Driver, software</th>
              <th>Channel</th>
              <th>State</th>
              <th className="num">
                Last frame <span className="unit">s</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {status.nodes.map((n) => {
              const a = nodeLevel(n);
              const site = sites.get(n.site_id);
              const rows = Math.max(1, n.channels.length);
              return (
                <Fragment key={n.site_id}>
                  {(n.channels.length ? n.channels : [null]).map((ch, i) => (
                    <tr key={`${n.site_id}-${ch?.channel_id ?? 'none'}`} aria-selected={focus === n.site_id ? true : undefined} className={!n.online ? 'muted' : undefined}>
                      {i === 0 ? (
                        <>
                          <td rowSpan={rows} style={{ verticalAlign: 'top' }}>
                            <div style={{ fontWeight: 700 }}>{site?.name ?? n.site_id}</div>
                            {site ? (
                              <div className="mono ink2" style={{ fontSize: 12 }}>
                                {formatLatLon(site.lat_deg, site.lon_deg, 3)}
                                <br />
                                {formatMgrs(site.lat_deg, site.lon_deg, 4)}
                              </div>
                            ) : null}
                          </td>
                          <td rowSpan={rows} style={{ verticalAlign: 'top' }}>
                            <span className="statuscell">
                              <Status level={a.level} word={n.online ? (a.level === 'normal' ? 'Online' : 'Degraded') : 'Offline'} />
                            </span>
                            {n.connected_since ? <div className="ink2 mono" style={{ fontSize: 12 }}>since {dtg(new Date(n.connected_since))}</div> : null}
                            {!n.online ? <div className="ink2" style={{ fontSize: 12 }}>{offlineFor(n)}</div> : null}
                            {n.online && a.reasons.length ? (
                              <div className="ink2 wrap" style={{ fontSize: 12, whiteSpace: 'normal', minWidth: 150, maxWidth: 200 }}>
                                {a.reasons.join('; ')}
                              </div>
                            ) : null}
                          </td>
                          <td rowSpan={rows} className="num" style={{ verticalAlign: 'top' }}>
                            {fixed(n.latency_ms, 0)}
                          </td>
                          <td rowSpan={rows} style={{ verticalAlign: 'top' }}>
                            {n.clock ? (
                              n.clock.locked ? (
                                `${n.clock.source}, locked`
                              ) : (
                                <>
                                  <span className="status">
                                    <StatusSymbol level="caution" /> {n.clock.source}, unlocked
                                  </span>
                                  <div className="ink2 mono" style={{ fontSize: 12 }}>
                                    holdover {formatAge(n.clock.holdover_s)}
                                  </div>
                                </>
                              )
                            ) : (
                              '—'
                            )}
                          </td>
                          <td rowSpan={rows} className="num" style={{ verticalAlign: 'top' }}>
                            {clockOffset(n.clock?.est_offset_ns)}
                          </td>
                          <td rowSpan={rows} style={{ verticalAlign: 'top' }}>
                            <span className="mono">{n.driver ?? '—'}</span>
                            {n.software ? <div className="ink2" style={{ fontSize: 12 }}>{n.software}</div> : null}
                          </td>
                        </>
                      ) : null}
                      <td className="mono">{ch?.channel_id ?? 'no channels'}</td>
                      <td>{ch ? (n.online ? (ch.ok ? 'ok' : <span className="status"><StatusSymbol level="caution" /> failed</span>) : 'offline') : ''}</td>
                      <td className="num">{ch?.last_frame_age_s === null || ch?.last_frame_age_s === undefined ? '—' : ch.last_frame_age_s < 60 ? fixed(ch.last_frame_age_s, 1) : formatAge(ch.last_frame_age_s)}</td>
                    </tr>
                  ))}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="ink2" style={{ padding: '8px 12px' }}>
        Server queue: {plural(status.queue.captures_pending, 'capture')} pending, {plural(status.queue.jobs_running, 'job')} running, {status.queue.jobs_waiting} waiting.
      </p>
    </div>
  );
}
