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
  they say what the system does with the physics as modelled.
- Tested on Python 3.13 on little-endian x86-64 Linux only (the package declares 3.11
  or later). The big-endian decode path is tested only in mirror image.

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
- Sites and reference transmitters are generic city-centre coordinates; no real
  facility is modelled. A real deployment needs real, authorised reference
  transmitters (OPERATIONS.md section 8).

## 3. Antennas

- **The 0.72 m PETAL dish is electrically small at UHF.** At the 260 to 268 MHz
  downlinks (wavelength about 1.15 m) it is 0.63 wavelengths across: about 3.3 dBi with
  a beam about 120 degrees wide, a reflector rather than a focusing dish. A 1.2 m dish
  is 1.05 wavelengths (7.7 to 7.9 dBi); a 10 dBi beam antenna (helix or Yagi) with 3 dB
  polarisation loss detects about as well as the 1.2 m dish (RESULTS.md section 1).
  `petal-locate check` flags every dish channel under 3 wavelengths.
- **The parabolic pattern is an envelope, not a physical pattern** (ARCHITECTURE.md
  section 5): it integrates to more than isotropic power for dishes of about 1.6 to 3
  wavelengths (1.36 for 2.4 m at 260 MHz) and for large dishes (1.10 to 1.24 at Ku
  band). That overstates sidelobe pickup, which is conservative for interference. The
  shipped UHF antennas integrate below 1 (0.60, 0.78, 0.77).
- No measured antenna patterns; polarisation loss is one constant per channel.
- A wide beam hears every satellite above the horizon, so co-channel satellites
  (MUOS Pacific and MUOS Indo-Asia share a translation) give several copies in one
  channel. The correlator excludes the predicted self-peaks and checks the swapped
  pairing, but see section 7 for what remains.

## 4. Hardware, drivers and time

- **No driver has run on hardware.** The `soapy` driver was written against the
  documented SoapySDR Python API and tested only against a software stand-in; device
  arguments, gain handling, PPS behaviour and timestamp accuracy must be validated on a
  radio, against a known signal, before any TDOA from it is trusted. Without the
  SoapySDR bindings a soapy channel fails when it opens (checked).
- `petal-locate node` gives every channel of a site the same driver options, so a site
  whose channels need different device arguments (one radio per channel) needs a small
  launcher using `NodeAgent(driver_factory=...)`; none is shipped.
- The `sigmf` driver replays one `cf32_le` recording for every channel of the node and
  takes centre and time from the first capture segment; channels outside the recorded
  band fail every frame (`out_of_range`).
- **TDOA between sites needs GPS-disciplined clocks.** A site-clock error enters every
  cross-site TDOA one for one (1 us is 300 m of range difference). The configuration
  assumes 30 ns per site. In the simulator a 10 us error at one site put the
  uncalibrated fix 14 to 34 km off, outside its own ellipse, with a reduced chi-square
  of 0.14, so the fit gave no warning. FDOA needs the site oscillator to about 1e-11
  (the default `site_osc_frac`; 2.6 mHz at 260 MHz against FDOA sigmas of about 1 mHz),
  that is, the radio locked to a disciplined 10 MHz reference.
- **No disciplined clock is shipped.** `SystemClock` reports source `ntp` and locked
  without asking the operating system; `--clock gpsdo` only changes the reported source.
  `DisciplinedClock` is an extension point with no implementation, and the soapy
  driver's `time_sync=pps` and `lock_sensor` are untested. The server's
  unlocked-clock rule (no cross-site TDOA from an unlocked capture) is only as good as
  the lock state a node reports. The simulated demo nodes report `ntp`, locked.
- Reference calibration removes a clock offset between two sites only for pairs whose
  transponders relay the reference, and only as far as the offset is stable between
  calibrations (every 30 s, valid for 300 s by default).

## 5. Physics model

- **UT1 and polar motion.** UT1 = UTC + `dut1_s` (default 0), polar motion is ignored,
  and inertial orbits use a linear Earth rotation angle (within 5e-8 rad, 2 m at GEO, of
  SGP4's from 2024 to 2031). Precise operator ephemerides should be given as ECEF tables.
- **Linear transponder.** Below saturation a carrier's output is its input scaled; above
  it every carrier and the noise are scaled down together (power robbing). No
  intermodulation, AGC or ALC modes, no passband shape: an emitter whose centre is in
  the uplink passband is relayed whole, and relayed noise is flat over the bandwidth.
- **No terrain, multipath or blockage.** Direct terrestrial paths exist only inside the
  radio horizon (26 km for two 10 m antennas) and are free space.
- **Stationary emitter.** The solver assumes the emitter does not move; a moving
  emitter is neither simulated nor solved for.
- **No ionosphere in any result.** The simulator has an optional thin-shell ionosphere,
  off in every shipped scenario; the solver has no ionospheric term.
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
  capture lengthening.
- **TLE ephemeris sigmas are order-of-magnitude guesses** (RIC 2 / 20 / 10 km and
  0.3 / 0.3 / 0.7 m/s). With TLE-grade errors uncalibrated fixes are off by about 23 km
  (median) and calibrated ones depend on the distance to the reference (RESULTS.md
  section 2.2).
- The satellite-velocity consider parameter is referred to each row's own epoch, so a
  velocity error never builds up into a position error across rows spanning hours
  (0.02 m/s is about 72 m per hour); negligible over the 10 to 30 s spans tested.

## 6. Detection and verdict

- Detection thresholds in RESULTS.md assume a noise-like jammer on a quiet channel,
  with the tracker's start-up prior as its baseline.
- The adaptive baseline learns a signal that rises slower than about 6 dB per
  `baseline_tau_s / 2` (it is never detected); a level step over a whole window
  freezes the baseline and stays one detection until the tracker is rebuilt; an emitter
  over every guard bin from the first frame is invisible until the history takes over
  (60 s by default). In one integration run two jammers switched on within about 10 s of
  server start raised no event in 40 s.
- An authorised carrier's range is widened by its possible Doppler and oscillator
  offsets, so an interferer within about 150 to 250 Hz of an authorised carrier at UHF
  is attributed to the carrier unless it raises the carrier's power by more than
  `power_anomaly_db`. Anomaly decisions on carriers 1 to 3 bins wide are unreliable for
  steps under about 7 dB. An authorised carrier that is off at start-up and comes on
  later is reported as an anomaly (the plan carries no expected level).
- Few averages: at `navg` 1 or 2 with 1024 bins, false tracks appeared 60 to 600 s after
  start-up. Derived monitors keep `navg = 8` except for spans under 1024 Hz.
- Two signals that cross in frequency swap track ids.
- A configuration change rebuilds every tracker whose inputs changed (any change to the
  detection settings rebuilds all); they relearn their baselines.
- The `local` verdict needs a capable site, farther than `min_site_separation_km` from
  every sighting, that does not see the signal; otherwise the event stays
  `undetermined`. A local event's state reads `characterizing` because tasking captures
  once before the verdict settles.
- Classification looks at the strongest signal of a 20 kHz-class capture; in one live
  run the CW jammer was labelled `noise` because the adjacent reference carrier fell
  inside the classification capture.

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
  that whole 25 kHz channel covers the reference and gets no calibration. Tasking picks
  the reference nearest in frequency, not the one nearest the emitter.
- Calibration cancels only what the target and the reference have in common: with
  TLE-grade errors it helps less with distance from the reference (predicted 95 %
  semi-major 1.2 km about 300 km from Guam, 4 to 24 km at about 3,000 km; RESULTS.md
  section 2.2), and as rows accumulate the calibrated ellipse can
  shrink past the uncancelled residual (ephemeris-error scenario: NEES up to 6 after 24
  noise-free rounds).
- **Channels relayed by one satellite cannot be located** (the example's `solo-*`
  channels); near Hawaii only two of the three satellites are visible.
- **CW (unmodulated) jammers: FDOA only.** Uncalibrated the two FDOA rows are
  unobservable; calibrated the fix is coarse (about 19 km median, with an ellipse tens
  of kilometres long). When a channel also hears a second copy of the tone a few
  Doppler bins away (a co-channel satellite or a self-peak ridge), the FDOA scatters up
  to about 5 reported sigma.
- **Periodically swept jammers are always ambiguous**: the system says so and gives no
  position.
- Reported sigmas: the TDOA sigma of co-channel same-site pairs is about 17 % low;
  for coupled waveforms (a sawtooth chirp) near threshold both sigmas are 15 to 26 %
  low; for a signal much narrower than an unfiltered correlation band the TDOA sigma is
  about 2.3 times too large (tasking always passes the signal bandwidth).
- The estimator gives about 1.6 to 2 % false alarms per capture at the default 13 dB in
  a large UHF window; tasking's repeat-detection gate keeps them out of the solver and
  the verdict, at the cost of one extra round before a pair is used.
- When one uplink emitter is replaced by another on the same frequency with no gap, the
  event continues and the solver mixes rows from both emitters. After a gap of more
  than 30 s the earlier position is set aside.
- Fixes depend slightly on where solving starts (cold and warm solves of the same 400
  rows ended 8 m apart, about 1.25 km from truth).
- The planning map (`accuracy_map`) models no co-channel copy as noise, no waveform
  coupling, no chirp ambiguity and no multi-epoch fusion, and predicts CW with no band
  filter (pessimistic by about 10 dB of post-correlation SNR). See MATH.md section 10.

## 8. Server, network and operations

- **Event loop.** CAF and solver jobs run in threads and share the GIL with the event
  loop: worst loop lag 40 to 66 ms with seven nodes on a shared 4-core machine, so the
  50 ms budget is met most of the time, not always. A process pool would remove this.
- **Memory and history.** Events stay in memory until more than 1000 inactive ones
  exist; older ones are dropped from memory and `GET /events/{id}` then answers 404,
  although the store keeps them; no API reads stored history. The start-up heap is
  frozen for the garbage collector until shutdown.
- **State lost on restart.** Calibrations, repeat-detection tracks and channel
  exclusions live in memory; a restart or a configuration change starts them afresh.
- **Capture retention** is fixed at 512 MiB of saved IQ per data directory; there is no
  setting or flag for it, and every completed capture is saved.
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
- `/docs` (Swagger UI) loads from a CDN and does not work offline; the console itself is
  self-hosted.
- An accuracy map can overrun its 120 s limit by one latitude band; a console on a very
  slow link can be cut off (1013) if one large event takes more than 30 s to send.
- Console: at 1024 px wide the measurement table needs horizontal scrolling for its
  last columns; the map is Mercator (scale bar true only at the centre latitude); the
  accuracy heat ramp runs in opposite directions in the dark and light themes.
- Some public geodesy functions return NaN for NaN input instead of raising, against
  CONTRIBUTING rule 5 (an internal inconsistency; no API payload carries NaN).
