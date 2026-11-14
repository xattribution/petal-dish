# PETAL Locate console: design record

Process: `anti-ai-ui` (full depth) with `military-ui-conventions` as the domain read.
This file is the plan and the justification ledger; anything on screen without a line
here should be removed.

## Domain read

1. **Operational system** on a dim ops floor with wall projection and long shifts, plus
   planners at desks. Dark theme is the default; the light theme is for planners and
   briefings and must work as well. Both ship, chosen by setting (Auto follows the OS).
2. **The first job is "is anything wrong?"**: the first screen is the event queue (most
   urgent first) beside the map, with the network roll-up above the queue. No welcome,
   no KPI tiles.
3. Operators are **spectrum-monitoring and satcom staff**: they read analyzer displays
   (trace over waterfall, MHz axis to the hertz, dB relative to the noise floor,
   markers), frequency plans (transponder edges, authorized carriers) and link budgets.
4. Geolocation readers expect a **fix**: position in decimal degrees with hemisphere and
   datum (WGS 84), MGRS beside it, 95 % error ellipse (semi-axes and orientation), CEP50,
   which error dominates, and whether a second position fits equally (ambiguity).
5. Measurements are **TDOA in microseconds and FDOA in hertz to the millihertz**, with
   sigmas, correlation SNR, whitened residuals and whether calibration was applied.
6. **Time is Zulu first**, 24-hour, DTG for people (`131805Z NOV 26`), ISO 8601 in files.
   A clock strip shows each site's local time with its computed zone letter and offset.
7. **Status** is the Astro ladder (Off, Standby, Normal, Caution, Serious, Critical):
   color plus shape plus word, roll-ups show the most urgent. Affiliation colors are
   not used for categories.
8. The **marking banner** comes from `/meta` only (top and bottom, fixed, above every
   overlay); none when `marking` is null. Demo marking: UNCLASSIFIED.
9. **Simulation truth** is fictional and carries a SIMULATION tag wherever it appears.
10. Networks may be closed: fonts and Natural Earth data are bundled; no request leaves
    the origin.

## Plan

**Color tokens** (`src/styles/tokens.css`): tinted neutrals `--paper --surface
--surface-2 --surface-3 --rule --rule-strong --ink --ink-2 --ink-3`; one interactive
accent `--accent/--on-accent` (focus rings, primary buttons, links on hover); the Astro
ladder `--st-off … --st-critical` with `-edge` strokes for the light theme; marking
levels `--mark-*`; data colors for the map and plots only: `--fix` (estimated position,
ellipse, ellipse history), `--lop` (lines of position), `--heat-0…6` (one-hue accuracy
ramp), waterfall ramp from `--wf-lo` to `--wf-hi`.

**Type**: B612 (Airbus/ENAC cockpit face, designed for legibility on degraded displays)
for text; B612 Mono for identifiers, DTGs, MGRS and numeric columns that are compared
digit by digit (frequencies to the hertz). Sizes 12 / 13 / 15 / 20 px; weights 400 / 700.

**Primary screen** (Watch, 1280 x 800):

```
+----------------------------------------------------------------------------------+
|                               UNCLASSIFIED                                       |
| PETAL Locate  Watch 1  Spectrum 2  Network 3  Plan 4  Configure 5  Simulation 6   |
|                                       [roll-up] [link: Live 0.3 s] [theme] [token]|
| 131805Z NOV 26 | Honolulu, Hilo 0805W -10:00 | Tokyo, Naha, Seoul 0305I +09:00 +1 | ..|
+-------------------------------+--------------------------------------------------+
| Network: 6 of 7 nodes online  |                                                  |
|  [tri] Serious  hilo offline  |               world map (Pacific)                |
|  [dia] Caution  manila clock  |   ellipses, LOPs, sites, sub-points, footprints  |
| Events  4 active              |                                                  |
| [sq] EV-0042 UPLINK located   |                                                  |
|   301.0010-301.0090 MHz up    |                                                  |
|   20.35N 132.42E 95% 10.6x0.6 | [scale bar: 2000 km at the equator]              |
| [sq] EV-0043 ...              | [x] Fixes  [x] Sites  [x] Sub-points  [ ] Footpr.|
| Show 2 lost or closed         | 20.350 N 132.420 E WGS 84  MGRS 53Q ...           |
+-------------------------------+--------------------------------------------------+
|                               UNCLASSIFIED                                       |
```

Event dossier replaces the map on the right (`#/event/EV-0042`): verdict and advisory
first, then the evidence matrix, the fix, the event map, measurements, correlation
surface and cuts, ellipse history, transitions. The queue stays on the left so j/k move
between events.

**Signature element: the evidence matrix.** Sites as rows, satellites as columns, one
cell per receive channel showing its evidence state (seen, would see but does not,
could not see, stale) as shape plus word plus SNR. The uplink-or-local verdict of MATH.md
section 7 becomes visible as a pattern: a lit column across distant sites is uplink, one
lit row is local. Everything else stays quiet.

**Map** (review pass): Mercator, rotated so the antimeridian is an ordinary meridian.
Mercator is conformal, so the error ellipse keeps its true shape and its major-axis
azimuth reads correctly off the map at any latitude, and lines of position cross at their
true angle (the first build used plate carree, which at 49.5 N stretched east-west by
1.54 and turned a 10 degree major axis into an apparent 15 degrees). Layer toggles and the
legend sit in a strip under the map, never over data; a scale bar gives km at the center
latitude; labels are placed around their marks and dropped (sites, sub-points) when they
would collide, never for fixes or truth.

## Swap test

Generic "ops monitoring dashboard": slate-900 page, indigo accent, Inter, icon sidebar,
row of KPI tiles (Nodes 7/7, Events 4, Uptime 99.9 %), rounded cards with shadows, line
charts, a "Welcome back". Changes made against it:

- Tiles replaced by a two-line roll-up sentence with the Astro status of the worst item.
- Icon sidebar replaced by a text tab row with number shortcuts (1-6).
- Cards replaced by flush regions separated by rules; radius only on controls.
- Inter replaced by B612 / B612 Mono (documented legibility provenance).
- Selection is inverse video (solid ink fill), not a tinted accent.
- The dossier leads with the operator's decision (advisory) and the evidence matrix,
  not with charts.
- Neutrals are tinted toward the map's water, not slate.

## Ledger

| Element | Encodes | Source |
|---|---|---|
| Marking banner, top and bottom, caps, level color | Handling level of the deployment | `/meta.marking`; mil conventions, Marking banner |
| `--st-*` six colors with ring / ring-dot / circle / diamond / triangle / square | Astro status ladder (Off, Standby, Normal, Caution, Serious, Critical); shape for color-blind and grayscale reading | Astro UXDS status system; MIL-STD-1472H color redundancy |
| Status word in caps next to the symbol | The state name operators use | Astro; caps under one line |
| Event urgency mapping (uplink Critical, local Serious, undetermined Caution, lost Standby, closed Off) | Uplink affects every user of the transponder; local one site | MATH.md section 7; ARCHITECTURE.md section 7 |
| Node status mapping (offline Serious, clock unlocked or stale frames Caution, normal) | Network gaps degrade verdicts and TDOA | PROTOCOL.md A heartbeat and clock rules |
| Inverse-video selected tab / row (solid `--ink` fill, `--paper` text) | Current view, selected record | Owner rule O1 |
| `--accent` focus ring 2 px | Keyboard focus | WCAG 2.4.7, O2 |
| `--accent` fill on primary buttons (Save, Apply, Compute) | The one committing action of a form | O2 |
| Red (`--st-critical`) only in status symbols and error text | Danger / failure | B7, MIL-STD-1472H |
| `--fix` (magenta) on map and history plot | Estimated emitter position and its ellipse; no status meaning | Data color, distinct from status hues and 2525 fills |
| `--ink-3` series in the history plot | Ellipses of measurements later set aside | K18 |
| Dashed `--fix` outline | Ambiguous (mirror) solution | MATH.md section 9 ambiguities |
| `--lop` solid / dashed | TDOA / FDOA line of position; band is the ±2 sigma region | MATH.md section 11 |
| `--heat-0..6` one-hue ramp with km legend | Predicted 95 % semi-major axis (or CEP50); the step farthest from the page color = best located, in both themes | MATH.md section 10; dataviz sequential ramp |
| `--heat-ink-0/1` | Dominant-error letters legible on light and dark heat cells | Contrast |
| Letters on accuracy cells | Dominant error source (N noise, P ephemeris position, V ephemeris velocity, C clock, O oscillator, B TDOA bias, R reference, I ionosphere, T troposphere) | MATH.md section 9 `dominant_error` |
| Grayscale waterfall (`--wf-lo`→`--wf-hi`) | Power over time; saturated hues are reserved for status | Spectrum convention; B6 |
| Shaded band on spectrum trace (`--fix`) | Detection of an unauthorized signal (interference) | MATH.md section 7 detections |
| Bracket with carrier id on spectrum | Authorized carrier allocation | Frequency plan (Carrier) |
| Dashed vertical rules on spectrum | Transponder passband edges | Transponder |
| Evidence glyphs: filled bar seen, hollow bar would see but does not, short dash cannot see, dotted bar stale | Evidence state of one channel (not a status: bars keep them apart from the status shapes) | MATH.md section 7 |
| SIMULATION tag (inverse video, caps) | Simulated truth, never real | UI-BRIEF; mil conventions demo data |
| Hatched cells in look-angle and evidence tables | Below horizon / stale: data that does not exist | Domain read 3, 4 |
| B612 Mono runs | Ids, DTGs, MGRS, frequencies and numeric columns compared digit by digit | Required practices: Type |
| Caps on table headers | Dense column headers only | B9 exception |
| 3 px radius on inputs and buttons only | Marks what is a control; regions sit flush | O4, B11 |
| One shadow on popovers (layer menu, tooltips, dialogs) | True overlays | B3 |
| Motion: none, except the reconnect countdown text | Real state change only | B13 |
| Dark default, light theme by setting | Dim ops floor; planners and briefings | Domain read 1 |
| Caution note on parabolic antennas under 3 wavelengths | Behaves as a reflector, broad beam | UI-BRIEF capability 7; core.antennas |
| Caution diamond in the look-angle antenna column | `LookAngle.electrically_small` for that channel and satellite | UI-BRIEF capability 6, 7 |
| Mercator projection | Conformal: ellipse azimuth and LOP crossing angles are true on screen | MATH.md section 9 (azimuth clockwise from true north) |
| Scale bar, km at the center latitude | Distances on a zoomed map (ellipse axes, miss distance) | Chart convention |
| Legend strip under the map | Every layer can be toggled and has a legend, without covering data | UI-BRIEF Map |
| Bracket in the strip above the spectrum trace | Authorized carrier allocation and its id, clear of the trace | Frequency plan (Carrier) |
| "Captures" line in the dossier header | What tasking is doing (`Event.tasking`): next round, rounds, channels, the server's message | schemas.py TaskingStatus |
| Clock strip grouped by shared local time | One entry per zone in use, so every site's letter and offset fit at 1024 px | mil conventions, Time |
| Small squares on the correlation surface, and a peak table | The other peaks the correlator found (`CafDisplay.candidates`); "ambiguous" when they are too close in height | MATH.md section 6 |
| Dashed cross and ellipse on the watch map | Every other position of an unresolved ambiguity (`SolutionSummary.ambiguities`) | MATH.md section 9 |
| Correction column in the measurement table, reference named above it | `MeasurementRow.correction` (already subtracted; raw = value + correction) and `reference_carrier_id` | schemas.py MeasurementRow |
| Key of the reasons under the measurement table | Each full reason a row was not used, once, with its count; the row shows the short word | pipeline/tasking.py reasons |
| "What-if" uncertainty in the accuracy form | `AccuracyRequest.uncertainty`: this map only, the configuration is not changed | UI-BRIEF capability 6 |
| Configured site time zone in the clock strip | `Site.time_zone` when set (the tooltip says configured or inferred) | mil conventions, Time |
| Roll-up Off ring with "No data" | No status received yet: never a green all-clear without data; while the link is down the last level stays with "not current" | Astro Off = unavailable; UX-7 |
| "as of HH:MM:SSZ" in the queue and dossier header | The console is not live: ages keep counting, "now" and "capturing IQ now" give way | UX-8 |
| Screen error in the view area (heading, cause, Retry) | A screen that could not be loaded or failed to render; banners, tabs and link state stay up | Required practices: every state; UX-2 |
| No focus ring on `<main>` | Landing point after a tab or number key, not a control (Tab moves on into the view) | WCAG 2.4.3; UX-11 |
| Backdrop of the token dialog stops at the banner rows | The marking banners stay above every overlay, undimmed | mil conventions, Marking banner; UX-14 |
| DTG followed by `HH:MM:SSZ` | Event times that change by the second; the DTG itself stays standard | mil conventions, Time; UX-14 |
| Caution notice "Not a fix" in Location, caution diamond on the queue row | `SolutionOut.fit_consistent` false: the measurements do not fit one stationary transmitter; the enlarged ellipse is shown, never reported | UX-1 |
| Caution notice "Not a fix" with the server's reason, "Not a fix yet" on the queue row, "not a fix" in the ellipse history | `SolutionOut.is_fix` false for a consistent fit (`not_fix_reason`: no redundancy, an ellipse beyond the sanity bound, no convergence); shown, never reported | R4 |
| Dotted `--fix` ellipse with no fill, label "(not a fix)" | A solution that is not a fix on the maps (`is_fix` false, including an inconsistent fit's enlarged ellipse) | UX-1, R4; distinct from the solid fix and the dashed ambiguity |
| "Position withdrawn" heading with the reason in Location | The newest measurements admit no position, so the one published is withdrawn (transition "position withdrawn: ...") | R6 |
| "at least N kHz" on the queue row and in the dossier header, with an (i) | `width_lower_bound`: in every monitor window the detections fill it to within the receiver's roll-off, so the signal may be wider | ADAPT-5 |
| "Site clocks by calibration" notes under the Network table; "differs by satellite" under an offset | `SiteClockEstimate.message`: a clock fault (caution diamond) or a satellite position the per-satellite views blame instead (no status: the clock is fine) | R3 |
| "nearest to each cell" reference in the accuracy form | `AccuracyRequest.reference_carrier_id = "nearest"`: each cell calibrated as tasking would calibrate an emitter there | R7 |
| "nearest to each cell" as the starting reference (when the configuration has reference carriers) | Live tasking always calibrates against the nearest eligible reference, so the first map predicts what tasking does; the operator's choice is kept once made | closing D2 |
| Pair list under the accuracy result (ends, satellites, calibration), divided by rules | `AccuracyGrid.pairs`: what each pair is calibrated with | closing D2 |
| Caution notice "Uncalibrated" with the server's reason, "No measurement" for a pair that gives nothing | `AccuracyPairSummary.uncalibrated_cells` / `reference_reason` (some or all cells have no usable reference) and `reason` | closing D2 |
| "Ephemeris allowance" line under the 95 % ellipse, with an (i), and "widened for ephemeris doubt" on the ellipse line | `SolutionOut.ephemeris_allowance` and `ellipse_widening`: the satellites whose position sigma the solve widened, by what factor, the widened one sigma (written as the advisory writes distances, "8.0 km"), and how much longer the 95 % ellipse's major axis is than with the configured uncertainty | closing D1; MATH.md section 9 |
| Caution notice "Ephemeris in doubt"; caution diamond before a satellite at the largest allowed scale | An allowance at its largest scale (`capped`), or a satellite in doubt (`Event.doubted_satellite_ids`) that the solve's rows go through and that it did not widen for yet: the transmitter may lie outside the ellipse | closing D1 |
| Candidates table "One of N positions" | Every position of an unresolved ambiguity, none singled out as the fix | MATH.md section 9; UX-6 |
| Caution notice "Re-check" | `Event.recheck`: the position is being checked against new measurements | UX-5 |
| "Start", "First seen", "Detected again ... after a break of" in the dossier header | `onset_ns` (the time to report), the first detection and the latest break | UX-5 |
| "Received ... at <site> on <channels>" in place of the uplink range | A local signal: what is on the air at the site, the frequency to tune to | UX-3 |
| Gray series and dashed rule in the ellipse history, muted rows with a note | Solves whose measurements were set aside; where the current transmission's solves begin | K18 |
| "By calibration" column with caution diamond | `Status.site_clocks`: the site clock offset the reference calibrations measure | UX-10 |
| Caution lines "detection blind on ..." and "noise floor +N dB on ..." (Network, spectrum headers) | A window detection cannot use (frames refused), or whose floor a filling signal raised | SRV-1; K1 |
| Serious triangle with the server's sentence in the rail, Satellites table in Network | A satellite whose ephemeris the server cannot evaluate: nothing is located through it | ADAPT-7 |
| Letters I and T on accuracy cells | Ionosphere and troposphere as the dominant error | MATH.md section 9 media consider terms |
| "simulated time, N behind this computer" (ink-2) in the clock strip | A simulated world running from a fixed epoch; the red clock warning stays for a real server | K8 |
| "channel@sat" in the pair selects | A channel watching several satellites names the one a pair means | ADAPT-4 |
| Plan side panel at least 448 px at every width; (i) tips hidden with `display: none`; dossier header column `minmax(0, 1fr)` | At 1024 px the elevation table, a hidden tip and a long header line no longer widen their panel past the window | UX-13; final recheck |
