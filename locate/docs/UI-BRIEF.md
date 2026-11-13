# PETAL Locate — operator console brief

## Who uses it and where

Spectrum-monitoring and satcom operators on an ops floor, plus planners deciding where
to put sites and which satellites to pair. It is an **operational system**: dim rooms,
wall projection, long sessions. Dark theme first; a light theme must also work.

Load and follow the `anthropic-skills:anti-ai-ui` and
`anthropic-skills:military-ui-conventions` skills (the second is the domain read inside
the first). Conventions that are decided already:

- Marking banner comes from deployment configuration served by the API, never from
  source. The demo configuration marks UNCLASSIFIED. No banner when none is configured.
- Zulu time first, 24-hour, DTG for people (`DDHHMMZ MON YY`), ISO 8601 in exports. A
  clock strip shows Zulu, then each site's local time with its computed zone letter
  and numeric offset.
- Status uses the Astro ladder (colour plus shape plus word). Affiliation colours
  (MIL-STD-2525 blue/red/green/yellow fills) are not used as categories.
- Positions: decimal degrees with hemisphere letters and the datum (WGS 84); MGRS shown
  beside it for ground points. Distances in km, frequencies in MHz with Hz precision
  where it matters, time differences in microseconds, frequency differences in mHz/Hz.
- Self-host every asset (fonts, map data). The console must work with no internet.
- Demo data is fictional and says SIMULATION wherever simulated truth is shown.

## What the operator must be able to do

1. **See at a glance whether anything is wrong.** Roll-up status of nodes, clocks and
   active interference events, most urgent first.
2. **Understand an event.** For the selected event: which transponder and frequency,
   when it started, waveform class, and the uplink-or-local verdict with its evidence
   (a site-by-site table of who sees it and who does not), and the plain-language
   advisory (change channel or satellite versus move or shield the terminal).
3. **See where the emitter is and how sure the system is.** Map with the estimated
   position, the 95 % ellipse, lines of position per satellite pair, mirror solutions
   when ambiguous, and the numbers: coordinates, ellipse axes, CEP, what dominates the
   error, measurement table (pair, TDOA, FDOA, sigmas, correlation SNR, residual,
   calibration applied or not), the correlation surface and its two cuts, and how the
   ellipse has shrunk as measurements accumulate. In simulation, the true position and
   the miss distance.
4. **Watch the spectrum.** Live trace and waterfall per channel and monitor window,
   with the learned baseline, detections, transponder edges and authorised carriers
   marked. Hover for frequency and level.
5. **Check the network.** Per site: connection, latency, clock source, lock and
   estimated offset, driver, channels, age of last frame.
6. **Plan.** Look-angle table (site by satellite, with below-horizon made obvious),
   satellite footprints, and the accuracy map: choose satellite pairs, emitter
   assumptions, uncertainty assumptions and whether a reference emitter is used, and see
   predicted 95 % error over the region with the dominant error source.
7. **Configure without code.** Satellites (paste a TLE, or a geostationary slot, or
   Kepler elements; transponders), sites (location, clock, channels, antenna, pointing,
   monitor windows), carriers and reference emitters, processing settings. Validation
   errors appear on the field. Saving handles version conflicts. Import and export the
   whole configuration as YAML. A parabolic antenna smaller than three wavelengths at
   its operating frequency shows a caution explaining it behaves as a reflector, not a
   focusing dish.
8. **Drive the simulation** (only when the server has a scenario): add, move and edit
   emitters by clicking the map (jammer aimed at a satellite, or a local interferer near
   a site), choose waveform, power and antenna, inject truth errors (ephemeris offset,
   clock offset, oscillator error, ionosphere), load presets, reset.

## Map

World map centred on the area of interest (the default configuration is the Pacific,
so the antimeridian must render correctly). Offline vector land and borders, graticule,
pan and zoom. Layers: sites, satellite sub-points and footprints, events and solutions,
lines of position, accuracy heat map, simulation truth. Every layer can be toggled and
has a legend.

## Engineering constraints

- React, TypeScript, Vite. Source in `locate/ui/`, production build written to
  `locate/src/petal_locate/server/static/` and committed, so the Python package serves
  the console without Node.
- Spectrum frames arrive at about 2 Hz for about 20 windows: draw them on canvas outside
  React's render cycle.
- Absolute timestamps from the API that are nanoseconds arrive as strings; never put
  them through `Number`.
- Keyboard reachable, visible focus, works at 1280 px wide and up, degrades sensibly
  at 1024 px.
- A Playwright smoke test drives the console against `petal-locate demo`.
