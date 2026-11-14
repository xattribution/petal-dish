# PETAL Locate — what this proof of concept does not prove or do

PETAL Locate is a proof of concept. Everything below is a known limit of what was built
and of how it was checked, gathered from the build, verification and integration
reports and confirmed against the code. Numbers are from the simulator unless stated
otherwise; `docs/RESULTS.md` has the capability numbers and how they were produced.

## 1. Validation

- **Simulator only.** Every result, test and screenshot comes from the project's own
  physics simulator (`sim/world.py`) or from the simulated demo. Nothing has been
  compared with real satellites, real terminals, real interferers or real radios.
- **The simulator and the solver share geometry code** (`core.geometry`, `core.orbits`,
  `core.linkbudget`). The tests therefore prove the signal processing, estimation,
  protocol and system logic and the self-consistency of the model, not that the model
  matches the sky. To limit this, the signal model was checked once against an
  independent from-scratch implementation of MATH.md sections 2 to 5 in extended
  precision, built without those modules; it agreed (CW phase within 8e-7 cycles,
  amplitude ratio 1.00000 with 13.8 dB of power robbing, baseband frequency within
  4e-7 Hz, truth TDOA within 7e-18 s and FDOA within 2e-9 Hz). An error shared by the
  physics as written in MATH.md and both implementations would not be caught.
- The capability numbers in RESULTS.md are simulated with illustrative link parameters;
  they say what the system does with the physics as modeled.
- Tested on Python 3.13 on little-endian x86-64 Linux only (the package declares 3.11
  or later). The big-endian decode path is tested only in mirror image.
- Only the dependency versions of the development environment were tested (numpy 2.5,
  scipy 1.18, pydantic 2.14, FastAPI 0.162, uvicorn 0.54, websockets 17.4, httpx 0.28,
  sgp4 2.27, contourpy 1.4, PyYAML 6.0). The minimum versions in `pyproject.toml` are
  what the code is believed to need (websockets 15 for the node's proxy support, for
  example), not versions it was run against.

## 2. The example system

- **Illustrative numbers.** The link parameters (G/T -14 dB/K, SFD -136 dBW/m^2, 26 dBW
  saturated EIRP per 25 kHz channel, 400 K receivers), the 25 kHz channel frequencies,
  translations, antennas and powers in `config/pacific-uhf.yaml` are placeholders, not
  data for any real spacecraft or terminal.
- **Nominal orbital slots.** The three satellites sit at nominal longitudes (177 W,
  172 E, 75 E) with invented inclinations (4 to 5 degrees) and phases. Every
  time-of-day effect in RESULTS.md (when the uncalibrated fix has two candidates, which
  site pairs are chosen) depends on those inventions.
- **MUOS.** MUOS's WCDMA user uplink goes to the Ka-band feeder link and is not visible
  on the UHF downlink, so this method does not apply to it. It applies to the legacy
  UHF bent-pipe channels (the configuration models every channel as a bent pipe).
- Sites and reference transmitters are generic city-center coordinates; no real
  facility is modeled. A real deployment needs real, authorized reference
  transmitters (OPERATIONS.md section 8).

## 3. Antennas

- **The 0.72 m PETAL dish is electrically small at UHF.** At the 260 to 268 MHz
  downlinks (wavelength about 1.15 m) it is 0.63 wavelengths across: about 3.3 dBi with
  a beam about 110 degrees wide (109 to 112 degrees from `core.antennas.hpbw_deg`), a
  reflector rather than a focusing dish. A 1.2 m dish
  is 1.05 wavelengths (7.7 to 7.9 dBi); a 10 dBi beam antenna (helix or Yagi) with 3 dB
  polarization loss detects about as well as the 1.2 m dish (RESULTS.md section 1).
  `petal-locate check` flags every dish channel under 3 wavelengths.
- **The parabolic pattern is an envelope, not a physical pattern** (ARCHITECTURE.md
  section 5): it integrates to more than isotropic power for dishes of about 1.6 to 3
  wavelengths (1.36 for 2.4 m at 260 MHz) and for large dishes (1.10 to 1.24 at Ku
  band). That overstates sidelobe pickup, which is conservative for interference. The
  shipped UHF antennas integrate below 1 (0.60, 0.78, 0.77).
- No measured antenna patterns; polarization loss is one constant per channel.
- A wide beam hears every satellite above the horizon, so co-channel satellites
  (MUOS Pacific and MUOS Indo-Asia share a translation) give several copies in one
  channel. The correlator excludes the predicted self-peaks and checks the swapped
  pairing, but see section 7 for what remains.

## 4. Hardware, drivers and time

- **No driver has run on hardware.** The `soapy` driver was written against the
  documented SoapySDR Python API and tested only against a software stand-in; device
  arguments, gain handling, PPS behavior and timestamp accuracy must be validated on a
  radio, against a known signal, before any TDOA from it is trusted. Without the
  SoapySDR bindings a soapy channel fails when it opens (checked).
- Per-channel driver options (`--channel-option`, `--node-config`) are laid over options
  every channel shares. The node checks the merged options of each channel it names
  before it dials out; a channel the server assigns without options of its own gets the
  shared ones only, and if those lack an option the driver requires (the sigmf `path`,
  for example) it fails when it opens (the node warns about this at start-up). One radio
  per channel with the `soapy` driver has not been run on hardware.
- The `sigmf` driver replays `cf32_le` recordings (one per channel when the channels
  are given their own `path`, else one for every channel) and takes center and time
  from the first capture segment; channels outside their recording's band fail every
  frame (`out_of_range`).
- **TDOA between sites needs GPS-disciplined clocks.** A site-clock error enters every
  cross-site TDOA one for one (1 us is 300 m of range difference). The configuration
  assumes 30 ns per site. In the simulator a 10 us error at one site put the
  uncalibrated fix about 31 km off (median over eight times of day, RESULTS.md section
  5), mostly outside its own ellipse, with a reduced chi-square of 0.14 in the run
  examined, so the fit gave no warning. FDOA needs the site oscillator to about 1e-11
  (the default `site_osc_frac`; 2.6 mHz at 260 MHz against FDOA sigmas of about 1 mHz),
  that is, the radio locked to a disciplined 10 MHz reference.
- **No disciplined clock is shipped.** `SystemClock` reports source `ntp` and locked
  without asking the operating system; `--clock gpsdo` only changes the reported source.
  `DisciplinedClock` is an extension point with no implementation, and the soapy
  driver's `time_sync=pps` and `lock_sensor` are untested. The server's
  unlocked-clock rule (no cross-site TDOA from an unlocked capture) is only as good as
  the lock state a node reports. (The simulated demo nodes report `sim`, locked.)
- The per-site clock estimate of `/status` (section 7) is a cross-check, not a clock
  monitor: it rests on reference calibrations and clock checks, which run only while an
  event is being tasked (a quiet system checks no clock).
- Reference calibration removes a clock offset between two sites only for pairs whose
  transponders relay the reference, and only as far as the offset is stable between
  calibrations (every 30 s, valid for 300 s by default).

## 5. Physics model

- **UT1 and polar motion.** UT1 = UTC + `dut1_s` (default 0), polar motion is ignored,
  and inertial orbits use a linear Earth rotation angle (within 5e-8 rad, 2 m at GEO, of
  SGP4's from 2024 to 2031). Precise operator ephemerides should be given as ECEF tables.
- **Linear transponder.** Below saturation a carrier's output is its input scaled; above
  it every carrier and the noise are scaled down together (power robbing). No
  intermodulation, AGC or ALC modes, no passband shape: an emitter whose center is in
  the uplink passband is relayed whole, and relayed noise is flat over the bandwidth.
- **No terrain, multipath or blockage.** Direct terrestrial paths exist only inside the
  radio horizon (26 km for two 10 m antennas) and are free space.
- **Stationary emitter.** The solver assumes the emitter does not move; a moving
  emitter is neither simulated nor solved for.
- **Ionosphere and troposphere are carried as uncertainty, not corrected.** The
  measurement model is vacuum light time; the solver and the accuracy map carry one
  vertical electron content and one zenith tropospheric delay per ground end as
  consider parameters (defaults 30 TECU with correlation 0.5 between ends, and 2.4 m
  with correlation 0.9; MATH.md sections 3 and 9). Sizes at UHF: 30 TECU delays one leg
  by 445 ns at the zenith and 1.24 us at 10 degrees elevation at 301 MHz (597 ns and
  1.66 us at 260 MHz); 2.4 m of troposphere by 8 ns at the zenith, 45 ns at 10 degrees
  and 82 ns at 5 degrees, in every band. Calibration cancels the downlinks but not the
  difference between the emitter's and the reference's uplinks: at 30 TECU the South
  China Sea rows calibrated against Guam keep -1,111 and -954 ns, the Philippine Sea
  rows 35 and 464 ns, against CAF noise of about 100 ns (RESULTS.md section 2.4). The
  consider terms keep fixes inside their ellipses at the levels tested but widen the
  UHF ellipses (RESULTS.md sections 2.4 and 3).
  - No nominal correction (Klobuchar, a TEC map) is applied, so the sigma must cover the
    whole delay. A day above the configured sigma (low-latitude daytime at solar
    maximum can exceed 60 TECU) gives ellipses that are too small (RESULTS.md section
    3); `processing.uncertainty.iono_vtec_tecu` must then be raised.
  - One thin shell at 350 km and one VTEC per ground end: no horizontal gradients along
    a path, no scintillation, no higher-order terms, no refraction of the elevation; the
    Black and Eisner tropospheric mapping is crude below about 5 degrees.
  - The simulator applies one VTEC and one zenith delay at every ground end, so the Monte
    Carlo tests only the fully correlated case of the per-end model, and the shipped
    scenarios have neither medium (their ellipses are cautious).
- **Low orbit.** The CAF assumes the differential delay drifts by less than `0.5 / B`
  and the Doppler by less than `1 / T` over a capture; tasking shortens captures to
  keep that, but low-orbit operation was exercised only in unit tests
  (`leo-example.yaml`). In low orbit: the carrier widening of the detector grows to
  several kHz; event identity uses nominal translations, so a narrowband signal can
  split into one event per satellite; the relay rise of each channel is fixed when the
  configuration is installed; `measurement_windows` can be loose by a cell or two and
  can miss a co-visible sliver thinner than about 1e-3 degree; a low-orbit TLE is
  smooth enough for the light-time code (1e-6 m) only within about 5 days of its element
  epoch; and the Monte Carlo driver lacks tasking's Doppler sample-rate rule and
  capture lengthening. The repeat-detection gate's FDOA-rate bound
  (`|v|^2 / r_min + |a|` per leg, MATH.md section 6) holds for every orbit but is a strict
  worst case: on `leo-example.yaml` the largest true rate is 0.19 of it, so the gate
  rejects correlator false alarms only weakly there, and captures shorten to about
  58 ms. A rate predicted from the current fix would be tighter but could refuse correct
  rows after a wrong fix.
- **TLE ephemeris sigmas are order-of-magnitude guesses** (RIC 2 / 20 / 10 km and
  0.3 / 0.3 / 0.7 m/s). With TLE-grade errors uncalibrated fixes are off by about 23 km
  (median) and calibrated ones depend on the distance to the reference (RESULTS.md
  section 2.2).
- The satellite-velocity consider parameter is referred to each row's own epoch, so a
  velocity error never builds up into a position error across rows spanning hours
  (0.02 m/s is about 72 m per hour); negligible over the 10 to 30 s spans tested. A
  calibrated row's reference partial is moved to the row's epoch too (the satellite
  moves by the velocity error times the reference's age, up to the allowed 300 s); the
  planning map evaluates the reference at the target epoch and leaves that out.

## 6. Detection and verdict

- Detection thresholds in RESULTS.md assume a noise-like jammer on a quiet channel,
  with the tracker's start-up prior as its baseline.
- The adaptive baseline learns a signal that rises slower than about 6 dB per
  `baseline_tau_s / 2` (it is never detected); a level step over a whole window
  freezes the baseline and stays one detection until the tracker is rebuilt.
- A signal already on when a tracker starts is found against the start-up floor as long
  as it leaves free the bins below the lowest floor quantile tried (1.5 % of 1024 bins,
  MATH.md section 7). One that fills a whole window is learned as that window's noise;
  the server then flags the window's floor as raised, but only when the channel has
  another window of the same span that had a frame in the last 30 s (single-window
  channels, as in `ku-adjacent.yaml`, are never judged), and a signal filling every
  window of a channel is not flagged. A receiver whose gain changes per tuning (AGC)
  could raise false flags. After a learned window-filling signal goes away, that
  window's baseline stays high until its history relearns (up to half of
  `baseline_tau_s`).
- A new event is held for up to one frame period (at most 1 s) so that fragments
  reported by several windows in the same instant raise one event.
- An authorized carrier's range is widened by its possible Doppler and oscillator
  offsets, so an interferer within about 150 to 250 Hz of an authorized carrier at UHF
  is attributed to the carrier unless it raises the carrier's power by more than
  `power_anomaly_db`. Anomaly decisions on carriers 1 to 3 bins wide are unreliable for
  steps under about 7 dB. An authorized carrier that is off at start-up and comes on
  later is reported as an anomaly (the plan carries no expected level).
- Few averages: at `navg` 1 or 2 with 1024 bins, false tracks appeared 60 to 600 s after
  start-up. Derived monitors keep `navg = 8` except for spans under 1024 Hz.
- Two signals that cross in frequency swap track ids.
- A configuration change rebuilds every tracker whose inputs changed (any change to the
  detection settings rebuilds all); they relearn their baselines.
- The `local` verdict needs a capable site, farther than `min_site_separation_km` from
  every sighting, that does not see the signal; otherwise the event stays
  `undetermined`. A local event reads `detected`: tasking captures once before the
  verdict settles, and nothing is captured once it is judged local.
- Classification looks at the strongest signal within four times the event's width
  around it (at least 10 Hz wide), cut out of the primary capture; another carrier
  inside that band can still be classified in the event's place.

## 7. Location

- **Two candidate positions.** With satellites inclined only 4 to 5 degrees, two
  hyperbolas can cross twice inside the region; uncalibrated, this happened at about
  half the times of day and the system shows an unresolved ambiguity (calibration
  removed it in every simulated case). With zero inclination the north-south mirror
  can be resolved only by the region, never by the measurements.
- **The reference must share the interferer's transponders.** A reference calibrates
  only pairs whose two transponders both relay it, and tasking does not use a
  reference whose band overlaps the event's uplink range. The example has one
  reference per shared channel, inside the channel users share, so a jammer that fills
  that whole 25 kHz channel covers the reference and gets no calibration. With several
  references in a channel tasking prefers the one nearest the event's current fix
  (nearest in frequency before a fix); the Monte Carlo driver takes the one nearest the
  emitter, and the planning map either the one named or, with `nearest`, the one
  nearest each cell. A reference also has to see
  every satellite the pairs use: one placed where MUOS Indo-Asia is below its horizon
  (east of about 160 E) leaves that satellite's pairs uncalibrated.
- Calibration cancels only what the target and the reference have in common: with
  TLE-grade errors it helps less with distance from the reference (predicted 95 %
  semi-major 1.2 km about 300 km from Guam, 4.4 to 24 km at about 3,000 km; RESULTS.md
  section 2.2) and depends on the direction (a reference 1,300 km away gave 2.3 to 3.3
  km due east or west, 6.8 to 10 km due north or south; RESULTS.md section 2.5). The
  uncancelled uplink ionosphere also grows with distance (section 5 above). As rows
  accumulate, the calibrated ellipse can shrink past the uncancelled part of a satellite
  error larger than its configured sigma: before the ephemeris allowance (below), the
  ephemeris-error scenario run live from 03:00 UTC had the truth outside the ellipse
  from 46 rows on in every snapshot for five minutes (180 to 240 m off a 95 % semi-major
  of 240 to 270 m) while the event stayed located.
- **Ephemeris allowance.** The clock checks name the satellites whose position is worse
  than configured, and the live solves widen those satellites' position sigma by a
  factor sized from the calibrations (MATH.md section 9), so the ellipse covers the
  error: live from 03:00 and from 15:00 UTC the ephemeris-error event was located in
  every snapshot over 9.5 minutes (8 to 400 rows), the two satellites' sigmas widened 8
  and 9.5 times, and the truth was inside the ellipse in all 76 snapshots (6 to 72 m
  off, 95 % semi-major 290 to 373 m, 15 to 30 % larger than without the allowance). Its
  limits:
  - it acts only on satellites the clock checks name, so only while an event is tasked
    and only for errors that make a site's timing differ between satellites by at least
    the doubt threshold (the largest of 1 us, ten times the site's clock sigma and three
    times its configured site-dependent ephemeris allowance);
    a satellite the sites see from a narrow angle shows some directions weakly (MUOS
    Indo-Asia, seen only from the Asian sites, showed an 8 km in-track error as 0.8 km
    in an offline check) and its error can go unnoticed and uncovered;
  - the calibrations give the smallest error that explains them: when only one of two
    satellites that see the network alike is off, the error is shared between them (8 km
    on MUOS Pacific alone read as about 5 km on it and -3 km on the 172 E satellite);
    their difference, which a calibrated pair through both feels, is right, and their
    combined allowance covers it, but a pair through the off satellite and a third one
    has less margin;
  - only the position sigma is widened: a velocity error beyond its sigma is neither
    seen nor covered;
  - the scale rises at once but falls back only when the calibrations that sized it
    have aged out (`calibration.max_age_s`, 300 s), so after an ephemeris update the
    ellipses stay wide for up to five minutes;
  - the evidence carries the configured media; ionospheric differences between sites
    beyond them can read as a satellite error and widen an ellipse more than needed
    (never less);
  - the factor stops at `calibration.ephemeris_allowance_max_scale` (20 by default);
    beyond it, or when the calibrations cannot size the error, the advice keeps warning
    that the transmitter may lie outside the ellipse.
- **Channels relayed by one satellite cannot be located** (the example's `solo-*`
  channels); near Hawaii only two of the three satellites are visible.
- **CW (unmodulated) jammers: FDOA only.** Uncalibrated the two FDOA rows are
  unobservable; calibrated the fix is coarse (about 16 km median, 95th percentile 50 km,
  with an ellipse tens of kilometers long). A position from two FDOA rows has no
  redundancy and is shown but is not a fix (such fits missed by 156 to 223 km outside
  84 to 95 km ellipses). When a channel also hears a second copy of the tone a few
  Doppler bins away (a co-channel satellite or a self-peak ridge), the FDOA scatters up
  to about 5 reported sigma; fits with spare measurements are then flagged inconsistent
  (reduced chi-square 5 to 20) rather than located, which is honest but slow: a CW may
  take many rounds to locate, or not be located at all.
- **Periodically swept jammers are ambiguous, as a rule**: a periodic sweep correlates
  with itself one period later and, through its line spectrum, at steps in delay and
  Doppler, so a pair whose search window holds more than one of those peaks gives no
  measurement; when every pair does, the system says so and gives no position (live,
  fresh at 03:00 and at 03:26 UTC: five and four minutes of ambiguous correlations). A
  calibrated pair whose window is narrow enough to hold one peak measures it correctly:
  live, an event reopened at about 03:26 UTC kept honolulu-pac as its primary, its pairs
  with the muos-io channels gave single peaks, and it was located with a 10 to 12 km by
  0.3 km ellipse that held the truth (misses 0.05 to 1.9 km), while the uhf-172e pairs
  stayed ambiguous. Whether that happens depends on the time of day and on which pairs
  tasking uses.
- Reported sigmas: co-channel same-site pairs carry the coherent self-peak term of
  MATH.md section 6 (their scatter over reported sigma is 1.02 for TDOA and FDOA, 100
  trials, RESULTS.md section 3; it was 1.08 to 1.11 without the term). The term is exact
  when the copies add in phase, as at one site in the simulator; with real separate
  dishes it is a cautious bound. For coupled waveforms (a sawtooth chirp) near
  threshold both sigmas are 15 to 26 % low; for a signal much narrower than an
  unfiltered correlation band the TDOA sigma is about 2.3 times too large (tasking
  always passes the signal bandwidth). The per-channel SNR split behind the shared
  capture noise attributes a co-channel copy heard in one channel partly to the other
  channel.
- The estimator gives about 1.6 to 2 % false alarms per capture at the default 13 dB in
  a large UHF window; tasking's repeat-detection gate keeps them out of the solver and
  the verdict, at the cost of one extra round before a pair is used.
- **Another transmitter on the same frequency.** When one uplink emitter is replaced by
  another, the pairs' measurements jump further than the satellites' motion allows and
  the earlier rows are set aside (MATH.md section 6); a break shorter than 30 s, or a
  change of waveform class, power or width, puts the position under re-check, and after
  a longer break it is set aside. The change is seen only through capture rounds: until
  the next round's pairs repeat at the new values, the old position stays published as
  located, with its report action (live, 15 to 40 s after an instant swap, at 1,000 and
  1,600 km from the new transmitter). A replacement that no earlier pair measured (the
  first transmitter gave no usable correlation) is recognized by the change of class,
  power or width it brings, and its start time is the first detection of its own pairs.
  Two transmitters on the air at once and detected
  alternately are not separated: their mixed fit fails the consistency rule (below), or
  the solve fails and the published position is withdrawn.
- **Fit consistency.** A fit with reduced chi-square above 5 and a chi-square tail
  probability below 0.001 is shown with its ellipse enlarged by the square root of the
  reduced chi-square and is never reported as located. The rule catches rows that do
  not fit one stationary transmitter; it cannot catch a systematic error the rows agree
  on (a 10 us clock fault gave a reduced chi-square of 0.14, section 4).
- **A fix needs redundancy and a bounded ellipse.** A position from exactly as many
  measurements as unknowns (`dof = 0`) cannot be checked against itself, and one whose
  95 % semi-major axis exceeds 1,000 km (or a quarter of the region's smaller extent)
  pins nothing down; both are shown as "not a fix" and never make an event located. The
  bound is a sanity limit, not a quality target: a consistent fit with spare rows and a
  950 km ellipse is still a fix.
- **Site clock estimate.** The `site_clocks` estimate of `/status` fits clock offsets
  from the calibration corrections and the same-satellite clock checks, which tell a
  late clock (alike through every satellite) from a satellite position error (through
  one satellite only). A site checked through only one satellite gets a caution that
  names both explanations until a check through a second satellite arrives (about a
  minute in the ephemeris-error scenario); two sites whose difference alone is known
  (for example two sites reached only through the same satellite from one channel) are
  both flagged on their difference, with a message saying one of the two clocks is off.
  While a clock fault is new, calibrations from before it still count until they are
  replaced or age out (`calibration.max_age_s`, 300 s by default): the fault's site is
  then shown with a smaller offset, or as one of such a pair, for up to a few minutes. A site whose offset differs between satellites is not flagged,
  and no site of its group is judged on an offset smaller than that difference, so a
  real clock fault smaller than a satellite disagreement the group shows is not
  flagged. Live, the clock-fault scenario flags Naha at -10.0 us within 20 s ("agree
  through two satellites") and widens no ellipse, and the ephemeris-error scenario
  raises no caution and names muos-pac and uhf-172e instead (ephemeris allowance above).
- Fixes depend slightly on where solving starts (cold and warm solves of the same 400
  rows ended 8 m apart, about 1.25 km from truth).
- The planning map (`accuracy_map`) models no waveform coupling, no chirp ambiguity, no
  reference age and no multi-epoch fusion (one TDOA and one FDOA per pair). It counts
  co-channel copies as noise, band-limits a CW as the estimator does, and carries the
  media and the shared capture noise (MATH.md section 10).

## 8. Server, network and operations

- **Event loop.** CAF and solver jobs run in threads and share the GIL with the event
  loop: worst loop lag 40 to 66 ms with seven nodes on a shared 4-core machine, so the
  50 ms budget is met most of the time, not always. A process pool would remove this.
- **Memory and history.** Events stay in memory until more than 1000 inactive ones
  exist; older ones are dropped from memory and `GET /events/{id}` then answers 404,
  although the store keeps them; no API reads stored history. The store keeps every
  event for ever unless `--history-max-age-s` or `--history-max-events` is set; pruning
  works on the store only, by last update, and is not undone. The start-up heap is
  frozen for the garbage collector until shutdown.
- **State lost on restart.** Calibrations, repeat-detection tracks and channel
  exclusions live in memory; a restart, a configuration change or (simulated) a
  scenario load starts them afresh.
- **Capture retention.** Every completed capture is saved, up to
  `--capture-limit-bytes` (512 MiB by default) per data directory, oldest deleted first;
  there is no choice of which captures to keep.
- **Simulated time.** `petal-locate demo --start` shifts the server's and the simulated
  nodes' clocks by one offset; log lines keep the host time. A store written under
  `--start` holds simulated times, so a later server without it may see those events
  as old (history limits included). Real nodes must never run with an offset.
- **Readiness after a configuration change.** After every `assign` all channels of a
  site are not ready until the node acknowledges, which waits for every new driver to
  open (up to 30 s); tasking and spectrum acceptance stop for that site meanwhile.
- **Slow uplinks.** A node needs about 44 kbit/s just to carry one 32 KiB capture chunk
  within three heartbeat periods; the heartbeat round trip includes time queued behind
  samples (seconds on a slow link), and WebSocket keepalive pings close a connection
  whose backlog exceeds 20 s. Spectrum frames need more (OPERATIONS.md section 2).
- **Security.** One shared API token for every console user, no user accounts, roles or
  audit trail, no rate limiting; node tokens are per site. TLS must be terminated by a
  proxy in front of the server; TLS and proxy paths were not tested. A peer that stops
  reading is abandoned after 3 s, but uvicorn holds its TCP connection until TCP gives
  up, so a fronting proxy should cap connections per client.
- **A server without an API token** (`serve` warns; the demo has one unless
  `--no-api-token`) is open to every process on the machine and to anything that can
  reach its port. Web pages are only partly kept out: the `Host` check stops DNS
  rebinding, the request-kind rule stops cross-site form posts, and the console socket
  refuses foreign origins, but nothing stops a local program or a browser extension.
  The demo's token travels in the printed address; the console keeps it in
  `sessionStorage` for the tab and removes it from the address bar.
- `/docs` (Swagger UI) loads from a CDN and does not work offline; the console itself is
  self-hosted.
- An accuracy map can overrun its 120 s limit by one latitude band; a console on a very
  slow link can be cut off (1013) if one large event takes more than 30 s to send.
- Console: at 1024 px wide the measurement table needs horizontal scrolling for its
  last columns; the map is Mercator (scale bar true only at the center latitude); the
  accuracy heat ramp runs in opposite directions in the dark and light themes.
- Some public geodesy functions return NaN for NaN input instead of raising, against
  CONTRIBUTING rule 5 (an internal inconsistency; no API payload carries NaN).
