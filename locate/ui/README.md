# PETAL Locate operator console

React + TypeScript + Vite. The production bundle is written to
`../src/petal_locate/server/static/` and served by the Python package at `/`; nothing in
it reaches outside the origin (fonts and Natural Earth map data are bundled).
Design record and justification ledger: [DESIGN.md](DESIGN.md).

## Commands

```bash
npm ci                      # dependencies (pinned; .npmrc sets legacy-peer-deps)
npm run dev                 # console + mock API on http://localhost:5173
PETAL_PROXY=http://127.0.0.1:8000 npm run dev    # console against a running server instead
npm test                    # Vitest: formatters, PLB1 decoder, API client, socket, map geometry, mock parity
npm run typecheck           # strict TypeScript, app and smoke test
npm run build               # type-check, then write the bundle to ../src/petal_locate/server/static/
npm run e2e                 # build, start the mock, walk every screen in both themes (Playwright)
PETAL_URL=http://127.0.0.1:8000 npx playwright test --trace off   # same walk against petal-locate demo
npm run gen:types           # src/api/types.gen.ts from python -m petal_locate.server.api.schemas
npm run gen:fixtures        # mock/fixtures/*.json from the numeric core and the server's modules (about 30 s)
npm run preview:mock        # the built bundle + mock API on http://127.0.0.1:4173 (no Vite)
```

The Python suite runs the same walk against a live `petal-locate demo`
(`tests/e2e/test_console.py`, marked slow, about a minute; skipped with the reason when
Node, these dependencies, the Playwright browser or the build are missing).

Smoke-test options: `PETAL_TOKEN` (API token of the server), `PETAL_SHOTS` (screenshot
directory, default `test-results/screens`), `PW_CHROMIUM` (Chromium executable when the
Playwright browsers are not installed). `MOCK_TOKEN=secret npm run preview:mock` makes the
mock require a token, to exercise the token prompt. `MOCK_PSD_HZ=20 npm run preview:mock`
streams spectrum ten times faster than the monitors' 2 Hz, so a minute of it soaks the
waterfall with ten minutes of frames (heap and frame timing were flat in the review run).

## Layout

```
src/api/        client.ts (REST, typed errors), ws.ts (one /ws/ui socket, backoff, subscribe),
                plb1.ts (binary spectrum frames), types.gen.ts (generated, committed)
src/state/      live store (status, events, link), shared resources (config, geometry, truth)
src/time/       ns-string arithmetic, DTG, zone letters and offsets, site zones
src/format/     units (MHz, kHz, us, Hz/mHz, km, dB), positions (DD + datum, MGRS)
src/domain/     Astro status mapping, monitor derivation, electrically-small dish caution
src/map/        canvas world map (d3-geo Mercator: conformal, so ellipse azimuths read true;
                antimeridian-safe; land and graticule drawn once per view into a cached layer, so
                an event update redraws only the overlays), legend strip, scale bar, Natural Earth loader
src/spectrum/   SpectrumEngine (trace + waterfall on canvas, outside React), the view
src/views/      Watch + event dossier, Network, Plan, Configure, Simulation
mock/           generate_fixtures.py (real core), fixtures/, server.mjs (REST + /ws/ui),
                validate.mjs (pydantic-phrased validation), vite-plugin.mjs, serve.mjs
e2e/            Playwright smoke walk (mock or live), fixes.spec.ts (mock only: failed screen
                load, dropped link, token prompt, inconsistent fit, ambiguity, local signal,
                event break, site clocks, satellites and blind windows, preset, panel widths)
```

## The mock

`mock/generate_fixtures.py` computes every number from the real core on
`config/pacific-uhf.yaml`: IQ synthesised with `core.signals` / `core.channel` along
`core.geometry` delays, correlated with `core.caf.caf_estimate`, solved with
`core.solver.solve` (reference calibration included), lines of position from
`core.lop`, spectra from `SimWorld.render_psd`, look angles and footprints from
`core.geometry`, accuracy grids from `core.accuracy.accuracy_map` through the server's
banded `compute_accuracy`. Advisories come from the server's `advisory.advise`, fit
consistency from its event book and site clock estimates from
`tasking.site_clock_offsets`. Each fixture is validated against `server/api/schemas.py`
before it is written. The events cover every state the console draws: a located uplink
that broke off for 41 s and was located again (set-aside ellipses, start of the current
transmission), an inconsistent fit from two transmitters detected alternately, an
unresolved ambiguity, a local signal, an undetermined one that one satellite alone
relays, a lost and a closed event. The console's default accuracy request (derived from
the configuration, `src/domain/plan.ts`) is always among the precomputed grids.

The mock server shifts fixture times to its start time, streams status every second,
spectrum at 2 Hz for subscribed windows (Gamma(navg) scatter, drift, detections) and
event updates every 5 s. Config saves use ETag / If-Match;
`POST /__mock/bump-config` simulates another operator's save (the next save gets 409).
Limits: geometry is the fixture epoch's; accuracy grids exist only for the precomputed
pair / kind / reference combinations (others answer 422 naming what exists); link budgets
only for the scenario emitters at their original positions; moving or adding an emitter
does not change the spectrum (switching it off, changing its EIRP or frequency does).
