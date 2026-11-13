// Offline Natural Earth land and borders (world-atlas, bundled as assets). Two scales:
// 1:110m while panning, 1:50m at rest. Also answers "which country is this point in" for
// site time zones.
import { geoContains } from 'd3-geo';
import type { Feature, FeatureCollection, Geometry, MultiLineString } from 'geojson';
import { feature, mesh } from 'topojson-client';
import type { GeometryCollection, Topology } from 'topojson-specification';
import land110Url from 'world-atlas/land-110m.json?url';
import land50Url from 'world-atlas/land-50m.json?url';
import countries50Url from 'world-atlas/countries-50m.json?url';

export interface World {
  land110: FeatureCollection | Feature;
  land50: FeatureCollection | Feature;
  borders50: MultiLineString;
  countries: Array<Feature<Geometry, { name: string }>>;
}

type Topo = Topology<{ land: GeometryCollection; countries: GeometryCollection<{ name: string }> }>;

let promise: Promise<World> | null = null;
let loaded: World | null = null;
const waiters = new Set<(w: World) => void>();

async function fetchTopo(url: string): Promise<Topo> {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`map data ${url}: ${r.status}`);
  return (await r.json()) as Topo;
}

export function loadWorld(): Promise<World> {
  if (!promise) {
    promise = Promise.all([fetchTopo(land110Url), fetchTopo(land50Url), fetchTopo(countries50Url)]).then(([l110, l50, c50]) => {
      const countries = feature(c50, c50.objects.countries) as FeatureCollection<Geometry, { name: string }>;
      const w: World = {
        land110: feature(l110, l110.objects.land),
        land50: feature(l50, l50.objects.land),
        borders50: mesh(c50, c50.objects.countries, (a, b) => a !== b),
        countries: countries.features.map((f) => ({ ...f, id: f.id })),
      };
      loaded = w;
      for (const cb of waiters) cb(w);
      waiters.clear();
      return w;
    });
  }
  return promise;
}

export function worldIfLoaded(): World | null {
  return loaded;
}

export function onWorld(cb: (w: World) => void): void {
  if (loaded) cb(loaded);
  else waiters.add(cb);
}

/** ISO 3166-1 numeric code of the country containing a point, or null (sea, unknown). */
export function countryAt(w: World, latDeg: number, lonDeg: number): string | null {
  for (const f of w.countries) {
    if (geoContains(f, [lonDeg, latDeg])) return f.id === undefined ? null : String(f.id);
  }
  return null;
}
