# What a PETAL dish network can detect and locate (simulated capability study)

**Every number in this document comes from the PETAL Locate simulator and models, run
on `config/pacific-uhf.yaml`: nominal orbital slots (MUOS Pacific at 177 W, a UFO-class
satellite at 172 E, MUOS Indo-Asia at 75 E, inclinations of 4 to 5 degrees invented),
illustrative link parameters (G/T -14 dB/K, SFD -136 dBW/m^2, 26 dBW saturated EIRP
per 25 kHz channel, 400 K receivers) and generic city-centre sites. None of it is
measured on real satellites, terminals or radios.** The simulator and the solver share
the project's geometry code, so these results show what the system does with the
physics as modelled, not that the model matches the sky. Raw numbers are in
`docs/results/*.json`; `scripts/capability_study.py` reproduces them (section 8).

## The short answer

The question: with small PETAL dishes in Hawaii, Japan, Korea and the Philippines
watching three UHF bent-pipe satellites, what can the network detect and locate, and
how accurately?

**Detection.** On its own, every site detects a noise-like jammer on the uplink of any
satellite it can see. With the 0.72 m PETAL dish the jammer needs about 11 dBW EIRP
toward the satellite if it is 10 kHz wide, or about 15 dBW if it fills a 25 kHz channel
(about 13 W and 32 W into an omnidirectional antenna). A 1.2 m dish or a 10 dBi beam
antenna detects about 3 to 3.5 dB weaker jammers: 7 to 8.5 dBW at 10 kHz. Thresholds
hardly vary with the site (under 1 dB), because a bent-pipe satellite relays the whole
footprint to every site that sees it. At these frequencies (260 to 268 MHz downlink,
wavelength about 1.15 m) the 0.72 m dish is 0.63 wavelengths across. It behaves as a
reflector with 3.3 dBi of gain and a 120 degree beam, not as a focusing dish, and the
configuration check flags every dish channel for this.

**Location.** A jammer that two or three satellites relay can be located from the
time and frequency differences between its copies. Without calibration, assuming the
satellite positions are known to operator grade (about 1 km), the 95 % error ellipse has
semi-axes of about 4 to 6 km by 0.6 km at 03 UTC. In the Monte Carlo of the whole chain
the median miss is 1.3 to 2.1 km and the 95th percentile 2.8 to 4.5 km. Calibrating
against a reference transmitter at a known place (the configured Guam or Sapporo
references, redesigned in this study so that all three satellites relay them well)
shrinks this tenfold: semi-axes of 0.25 to 1.8 km by 0.1 km, depending on place and
time of day. The median miss is then 140 to 280 m and the 95th percentile 0.3 to 0.95 km. With
satellite positions known only to TLE grade (tens of kilometres in-track), uncalibrated
fixes are off by about 23 km (median; 95th percentile 66 to 73 km). Calibration then
depends on distance from the reference: 0.4 km median near Guam (300 km from the
reference), 0.9 km in the Philippine Sea (1,500 km away), and a predicted 95 %
semi-major of 4 to 24 km at about 3,000 km (South China Sea, Korea, Sea of Japan). A
reference within about 1,300 km of every area of interest would keep the TLE-grade 95 %
semi-major to 1 to 6.5 km.

**What the network cannot do:**

- Locate anything on a channel that only one satellite relays (the "solo" channels).
  Every site watching that satellite detects such a jammer (51.7 to 55.9 dB-Hz for
  20 dBW), so the uplink-or-local verdict, which needs sightings at two distant sites,
  still works, but there is no second copy to compare with.
- Locate a CW (unmodulated) jammer without calibration. A CW has no bandwidth, so only
  frequency differences can be measured. Calibrated, a CW fix is coarse: about 19 km
  median miss, with an ellipse tens of kilometres long.
- Locate a periodically swept jammer at all. Every correlation is ambiguous, and the
  system says so instead of giving a position.
- Locate well near Hawaii. Only two of the three satellites see that area (75 E is
  always below Hawaii's horizon), so there is one pair. It is unobservable without
  calibration, and gives 3 to 7 km calibrated against the distant Guam reference, or
  0.5 to 1.7 km with a reference near Honolulu.
- Always choose between two candidate positions when uncalibrated. With satellites
  inclined only 4 to 5 degrees, two hyperbolas can cross twice inside the region. This
  happens at about half the times of day, and the system then shows an unresolved
  ambiguity. Calibration removed it in every simulated case. With zero inclination the
  north-south mirror can never be resolved by the measurements, only by the region.

**Sites.** Location accuracy is set by the satellites and the reference, not by the
number of sites. Any one of the five Asian sites, each with channels on all three
satellites, predicts within about 25 % of the full seven-site network, and dropping any
one site changes the prediction by at most about 20 %. The Hawaii sites alone see only
two satellites (calibrated 2 to 14 km, uncalibrated unobservable). More sites buy
redundancy, an uplink-or-local verdict without a correlation (it needs sightings at two
sites more than 200 km apart), and detection margin through bigger antennas. They do
not buy location accuracy.

## 1. Detection thresholds

The weakest jammer EIRP, in dBW toward the satellite, that the server's spectrum tracker
reports as one detection covering at least 80 % of the jammer's band in at least 3 of 4
independent 10 s looks. The jammer is noise-like, at 20 N 132 E. "10 kHz" is at 301.005
MHz and "25 kHz" fills the 301.000 MHz channel; both are in shared-1, relayed by all
three satellites. Every channel was given the antenna under test, pointed at its
satellite. The beam antenna is linear and carries the configured 3 dB polarisation loss;
the dishes carry none. Epoch 2026-01-01 03:00 UTC; reference carriers removed.

| site | satellite | 0.72 m dish 10 kHz | 0.72 m dish 25 kHz | 1.2 m dish 10 kHz | 1.2 m dish 25 kHz | 10 dBi beam 10 kHz | 10 dBi beam 25 kHz |
|---|---|---|---|---|---|---|---|
| Honolulu | MUOS Pacific | 10.8 | 14.8 | 7.4 | 11.3 | 7.9 | 11.8 |
| Honolulu | 172 E | 10.7 | 15.2 | 7.2 | 11.7 | 7.9 | 12.4 |
| Hilo | MUOS Pacific | 10.7 | 14.6 | 7.2 | 11.2 | 7.7 | 11.7 |
| Hilo | 172 E | 10.7 | 15.0 | 7.2 | 11.6 | 7.9 | 12.2 |
| Tokyo | MUOS Pacific | 10.9 | 15.0 | 7.3 | 11.5 | 8.2 | 12.2 |
| Tokyo | 172 E | 11.1 | 15.0 | 7.5 | 11.5 | 8.2 | 12.2 |
| Tokyo | MUOS Indo-Asia | 11.4 | 15.6 | 7.8 | 12.0 | 8.5 | 12.7 |
| Naha | MUOS Pacific | 11.0 | 15.1 | 7.6 | 11.6 | 8.1 | 12.1 |
| Naha | 172 E | 11.1 | 15.1 | 7.6 | 11.6 | 8.3 | 12.2 |
| Naha | MUOS Indo-Asia | 11.3 | 15.1 | 7.8 | 12.1 | 8.5 | 12.3 |
| Seoul | MUOS Pacific | 10.9 | 15.4 | 7.3 | 11.8 | 8.0 | 12.3 |
| Seoul | 172 E | 10.9 | 15.2 | 7.3 | 11.7 | 8.0 | 12.3 |
| Seoul | MUOS Indo-Asia | 11.2 | 15.2 | 7.8 | 11.8 | 8.4 | 12.3 |
| Busan | MUOS Pacific | 11.0 | 15.2 | 7.5 | 11.8 | 8.1 | 12.1 |
| Busan | 172 E | 11.2 | 15.2 | 7.6 | 11.6 | 8.3 | 12.3 |
| Busan | MUOS Indo-Asia | 11.1 | 15.4 | 7.7 | 11.8 | 8.2 | 12.5 |
| Manila | MUOS Pacific | 11.1 | 15.4 | 7.5 | 11.8 | 8.2 | 12.5 |
| Manila | 172 E | 11.0 | 15.1 | 7.4 | 11.6 | 8.1 | 12.3 |
| Manila | MUOS Indo-Asia | 11.2 | 15.1 | 7.5 | 11.6 | 8.3 | 12.3 |

Hawaii has no MUOS Indo-Asia row: 75 E is 39 to 45 degrees below its horizon (section
4.3).

- **Other definitions.** The link-budget "nominal" level, where the jammer's mean
  per-bin excess reaches 4.74 dB (half its bins cross the tracker's 6 dB threshold at
  `navg = 8`), lies 1.1 to 2.0 dB above the table. The tracker joins runs of bins
  separated by gaps of up to 2 bins, so a wide signal is confirmed somewhat below that
  level. 4.7 to 5.9 dB below the nominal level, the tracker already confirms scattered
  detections a few bins wide inside the jammer's band: the operator sees something, but
  not one signal with its true width.
- **Antenna gains.** At the downlink (260.0 to 267.4 MHz), from `core.antennas`: 0.72 m
  dish 3.3 to 3.5 dBi, 1.2 m dish 7.7 to 7.9 dBi, beam 10 dBi less 3 dB of polarisation
  loss. Both dishes are under 3 wavelengths (0.63 and 1.05). `SystemConfig.warnings()`
  gives one note per dish channel, 13 in all, "under 3 wavelengths ... behaves as a
  reflector with a broad beam, not as a focusing dish", and the console shows them.
- **Location within the footprint.** Moving the jammer from the sub-satellite point to
  5 degrees elevation adds up to 1.2 dB of uplink path loss.

## 2. Location accuracy over the region

Predicted by `core.accuracy.accuracy_map` (MATH.md section 10): one TDOA and one FDOA
per pair from a single 1 s capture, for the pairs the tasking rule picks for a 20 dBW,
10 kHz noise jammer at the point. The rule picks the primary channel by received C/N0
and one partner per other satellite. Each cell gives the 95 % semi-major axis / CEP50 in
km at 03 UTC; the last column gives the range of the semi-major over 03, 09, 15 and 21
UTC. Elsewhere in this document "an ellipse of a by b" means its 95 % semi-axes.
"Calibrated, Guam" uses the configured shared-1 reference. "Nearest ref" is the same reference design moved, as a
what-if, to the nearest of five generic city centres: Guam, Sapporo, Koror, Taipei,
Honolulu.

### 2.1 Operator-grade ephemeris (RIC one-sigma 100 / 1000 / 1000 m, as configured)

| point | satellites seeing it | pairs at 03 UTC | uncalibrated | calibrated, Guam ref | calibrated, nearest ref | dominant error (uncal / cal) | range over the day: uncal / cal Guam / cal nearest |
|---|---|---|---|---|---|---|---|
| Philippine Sea (20 N, 132 E) | 3 | honolulu-pac/naha-172e, honolulu-pac/seoul-io | 4.59 / 1.29 | 0.47 / 0.14 | 0.43 / 0.12 (Taipei, 1,209 km) | ephemeris position / reference | 4.6-51 / 0.37-0.55 / 0.31-0.46 |
| South China Sea (15 N, 115 E) | 3 | seoul-io/seoul-pac, seoul-io/seoul-172e | 5.95 / 1.66 | 0.47 / 0.13 | 0.51 / 0.14 (Taipei, 1,305 km) | ephemeris position / ephemeris velocity | 6.0-190 / 0.47-0.55 / 0.51-0.61 |
| Sea of Japan (40 N, 135 E) | 3 | honolulu-pac/naha-172e, honolulu-pac/seoul-io | 4.47 / 1.26 | 0.95 / 0.27 | 0.44 / 0.12 (Sapporo, 630 km) | ephemeris position / ephemeris position | 4.5-793 / 0.95-1.83 / 0.34-0.56 |
| near Guam (15 N, 147 E) | 3 | honolulu-pac/naha-172e, honolulu-pac/seoul-io | 4.34 / 1.22 | 0.38 / 0.11 | same (Guam, 296 km) | ephemeris position / reference | 4.3-294 / 0.24-0.38 / 0.24-0.38 |
| near Hawaii (20 N, 160 W) | 2 | honolulu-pac/naha-172e (the Seoul pair has no copy) | unobservable | 5.45 / 1.81 | 1.43 / 0.44 (Honolulu, 266 km) | - / ephemeris velocity | unobservable or about 11,000 / 2.8-6.7 / 0.54-1.74 |
| Korean peninsula (37 N, 127.5 E, inland) | 3 | honolulu-pac/naha-172e, honolulu-pac/seoul-io | 4.56 / 1.28 | 0.86 / 0.25 | 0.51 / 0.14 (Sapporo, 1,358 km) | ephemeris position / ephemeris position | 4.6-65 / 0.86-1.57 / 0.44-0.62 |
| Pacific east of Japan (35 N, 150 E) | 3 | honolulu-pac/naha-172e, honolulu-pac/seoul-io | 4.17 / 1.18 | 0.85 / 0.24 | 0.46 / 0.13 (Sapporo, 1,165 km) | ephemeris position / ephemeris position | 4.2-79 / 0.85-1.49 / 0.39-0.70 |

The uncalibrated upper ends (51 to 793 km) come from times of day when the two pairs'
hyperbolas meet at a shallow angle. The ellipse then grows long; in the full chain that
is when a second candidate appears (section 4.1).

### 2.2 TLE-grade ephemeris (2 / 20 / 10 km, 0.3 / 0.3 / 0.7 m/s, every satellite; the solver assumes the same)

| point | uncalibrated | calibrated, Guam ref | calibrated, nearest ref | distance to the Guam reference | range over the day: uncal / cal Guam / cal nearest |
|---|---|---|---|---|---|
| Philippine Sea | 84.9 / 23.8 | 4.41 / 1.25 | 2.86 / 0.83 (Taipei) | 1,538 km | 85-953 / 4.0-5.4 / 2.6-4.0 |
| South China Sea | 110.9 / 30.9 | 5.41 / 1.63 | 5.21 / 1.46 (Taipei) | 3,213 km | 111-3,383 / 4.4-5.4 / 5.2-6.2 |
| Sea of Japan | 77.5 / 21.8 | 9.29 / 2.64 | 2.33 / 0.66 (Sapporo) | 3,090 km | 77-11,217 / 9.3-24.3 / 2.3-2.6 |
| near Guam | 80.7 / 22.7 | 1.17 / 0.33 | same (Guam) | 296 km | 81-4,899 / 1.0-1.2 / 1.0-1.2 |
| near Hawaii | unobservable | 81.9 / 29.0 | 4.71 / 1.47 (Honolulu) | 5,911 km | unobservable or about 11,000 / 54-99 / 1.8-5.6 |
| Korean peninsula | 80.2 / 22.5 | 8.58 / 2.45 | 4.45 / 1.25 (Sapporo) | 3,120 km | 80-1,130 / 8.6-20.7 / 4.5-4.9 |
| Pacific east of Japan | 72.4 / 20.5 | 9.88 / 2.78 | 2.90 / 0.84 (Sapporo) | 2,442 km | 72-1,342 / 9.9-19.2 / 2.9-6.5 |

With TLE-grade ephemeris, calibration removes the part of the satellite error that the
jammer and the reference share. What is left grows with the distance between them, and
the geometry matters too: Taipei is nearer the South China Sea point than Guam but helps
no more. **Dominant error source:** uncalibrated, it is the satellite position at every
point and both grades. Calibrated, it is the reference's own measurement noise or the
ephemeris velocity near the reference, and the uncancelled ephemeris position farther
away.

### 2.3 Where a shared-channel jammer can be located at all (03 UTC, 95 % semi-major in km)

Each cell holds the prediction for the tasking rule's pairs. "unobs" means fewer than
two independent rows constrain the position: one satellite pair, whose uncalibrated FDOA
carries the oscillator offsets. Longitudes 160 E to 160 W are seen only by MUOS Pacific
and 172 E.

Operator-grade, uncalibrated:

| lat \ lon | 100 | 110 | 120 | 130 | 140 | 150 | 160 | 170 | 180 | -170 | -160 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| -10 | >100 | 9.2 | 7.5 | 7.1 | 6.8 | 6.6 | unobs | unobs | unobs | unobs | unobs |
| 0 | >100 | 7.3 | 6.0 | 5.7 | 5.4 | 5.1 | unobs | unobs | unobs | unobs | unobs |
| 10 | >100 | 6.4 | 5.2 | 5.0 | 4.7 | 4.4 | unobs | unobs | unobs | unobs | unobs |
| 20 | >100 | 5.8 | 4.8 | 4.6 | 4.4 | 4.2 | unobs | unobs | unobs | unobs | unobs |
| 30 | >100 | 5.6 | 4.7 | 4.5 | 4.3 | 4.1 | unobs | unobs | unobs | unobs | unobs |
| 40 | >100 | 5.6 | 4.7 | 4.5 | 4.4 | 4.3 | unobs | unobs | unobs | unobs | unobs |
| 50 | unobs | 5.0 | 4.9 | 4.7 | 4.6 | 4.6 | unobs | unobs | unobs | unobs | unobs |

Operator-grade, calibrated against the Guam reference:

| lat \ lon | 100 | 110 | 120 | 130 | 140 | 150 | 160 | 170 | 180 | -170 | -160 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| -10 | 1.4 | 1.2 | 1.2 | 1.2 | 1.1 | 1.2 | 5.6 | 5.8 | 6.3 | 7.1 | 8.3 |
| 0 | 1.0 | 0.8 | 0.8 | 0.7 | 0.7 | 0.7 | 3.6 | 3.9 | 4.5 | 5.3 | 6.4 |
| 10 | 0.7 | 0.5 | 0.5 | 0.5 | 0.4 | 0.4 | 2.2 | 2.8 | 3.5 | 4.4 | 5.4 |
| 20 | 0.8 | 0.6 | 0.6 | 0.5 | 0.4 | 0.5 | 2.9 | 3.3 | 3.9 | 4.7 | 5.5 |
| 30 | 1.3 | 0.8 | 0.7 | 0.7 | 0.7 | 0.7 | 5.4 | 5.4 | 5.6 | 6.0 | 6.4 |
| 40 | 2.0 | 1.1 | 1.0 | 0.9 | 1.0 | 1.0 | 9.0 | 8.5 | 8.3 | 8.3 | 8.4 |
| 50 | unobs | 1.3 | 1.3 | 1.3 | 1.3 | 1.4 | 14.2 | 13.1 | 12.4 | 12.1 | 12.1 |

TLE-grade, calibrated against the Guam reference:

| lat \ lon | 100 | 110 | 120 | 130 | 140 | 150 | 160 | 170 | 180 | -170 | -160 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| -10 | 19.7 | 16.3 | 15.7 | 14.7 | 13.2 | 13.2 | 75.8 | 80.2 | 88.6 | >100 | >100 |
| 0 | 13.0 | 10.3 | 9.6 | 8.8 | 7.2 | 6.7 | 47.2 | 53.5 | 63.8 | 77.6 | 95.4 |
| 10 | 9.0 | 6.2 | 4.6 | 3.4 | 2.4 | 2.3 | 24.2 | 36.9 | 50.6 | 65.6 | 82.8 |
| 20 | 10.2 | 7.5 | 6.1 | 4.7 | 2.9 | 4.0 | 34.7 | 44.4 | 55.7 | 68.1 | 81.9 |
| 30 | 16.8 | 10.0 | 8.6 | 6.9 | 6.0 | 7.9 | 72.0 | 73.8 | 78.5 | 85.0 | 92.9 |
| 40 | 27.6 | 10.9 | 9.9 | 9.2 | 9.8 | 12.0 | >100 | >100 | >100 | >100 | >100 |
| 50 | unobs | 12.7 | 12.5 | 13.0 | 14.4 | 16.9 | >100 | >100 | >100 | >100 | >100 |

## 3. Monte Carlo confirmation (full chain)

`sim.montecarlo.run_trials` with `draw_errors=True`: simulated IQ, then the CAF
estimator, the reference calibration and the solver. Every trial draws new satellite
ephemeris, oscillator, site-clock and per-pair TDOA-bias errors from the configured
one-sigma values, and the solver assumes the same values. 40 trials per row, 1 s
captures, 03 UTC, pairs by the tasking rule. "Inside" is the fraction of fixes inside
the solver's own 95 % ellipse; the ideal mean NEES is 2. With 40 trials the binomial
standard error of a 95 % containment is 0.034.

| point | ephemeris | calibration | median miss | 95th pct miss | inside own 95 % ellipse | mean NEES | predicted CEP50 |
|---|---|---|---|---|---|---|---|
| Philippine Sea | operator | none | 1.89 km | 3.05 km | 0.925 (37/40) | 2.31 | 1.29 km |
| Philippine Sea | operator | Guam ref | 0.14 km | 0.37 km | 0.975 (39/40) | 1.65 | 0.14 km |
| South China Sea | operator | none | 2.07 km | 4.48 km | 0.950 (38/40) | 2.21 | 1.66 km |
| South China Sea | operator | Guam ref | 0.17 km | 0.46 km | 0.850 (34/40) | 3.40 | 0.15 km |
| Sea of Japan | operator | none | 1.30 km | 4.02 km | 0.925 (37/40) | 2.30 | 1.26 km |
| Sea of Japan | operator | Guam ref | 0.24 km | 0.95 km | 0.900 (36/40) | 2.83 | 0.27 km |
| near Guam | operator | none | 1.28 km | 2.79 km | 0.975 (39/40) | 1.43 | 1.22 km |
| near Guam | operator | Guam ref | 0.14 km | 0.31 km | 0.950 (38/40) | 1.89 | 0.12 km |
| east of Japan | operator | none | 1.29 km | 3.50 km | 0.975 (39/40) | 1.95 | 1.18 km |
| east of Japan | operator | Guam ref | 0.28 km | 0.58 km | 0.950 (38/40) | 1.84 | 0.25 km |
| Philippine Sea | TLE | none | 23.1 km | 66.0 km | 0.975 (39/40) | 1.78 | 23.8 km |
| Philippine Sea | TLE | Guam ref | 0.92 km | 3.02 km | 0.950 (38/40) | 1.70 | 1.25 km |
| near Guam | TLE | none | 23.5 km | 72.7 km | 0.925 (37/40) | 2.64 | 22.7 km |
| near Guam | TLE | Guam ref | 0.39 km | 1.06 km | 0.950 (38/40) | 2.24 | 0.34 km |

The predicted CEP50 agrees with the measured median miss within 25 % in 12 of the 14
rows; the median of 40 misses itself scatters by about 18 %. The exceptions are the
uncalibrated Philippine Sea (1.89 km measured, 1.29 km predicted, with normal
containment, 37/40, and mean NEES, 2.3) and the TLE-grade calibrated Philippine Sea
(0.92 km against 1.25 km, the prediction cautious). The calibrated South China Sea row
has the right median but is over-confident: 34 of 40 inside, mean NEES 3.4, 2.9 standard
errors off. Both of its pairs are formed at Seoul with co-channel MUOS satellites, whose
TDOA sigma the correlator is known to understate by about 17 % (`sim_verify` report,
n = 100). None of the 03 UTC trials showed a second candidate.

## 4. What limits it

### 4.1 Two candidate positions

Noise-free truth measurements of the two default pairs at 20 N 132 E, solved with the
configured uncertainty (`limits.json`, `mirror`):

| epoch | shipped orbits (4 to 5 deg inclination) | all inclinations set to 0 |
|---|---|---|
| 03 UTC | one fix (the mirror is removed by the region) | unresolved: truth and its mirror (20 S, 132 E), equal chi-square |
| 09 UTC | unresolved: truth and (49.9 S, 128.7 E), chi-square 2.4 | unresolved: truth and the mirror |
| 15 UTC | one fix (region) | unresolved: truth and the mirror |
| 21 UTC | unresolved: truth and (8.5 N, 132.4 E), chi-square 0.08 (the second crossing of the two hyperbolas) | unresolved: truth and the mirror |

In the full chain the shipped scenarios showed a second, unresolved candidate in 16 of
32 uncalibrated trials (four of eight epochs; `scenarios.json`) and in none of the
calibrated trials. Calibration removes the oscillator offsets from the FDOA rows, which
then tell the crossings apart. With satellites in the equatorial plane only the region
of interest separates the north-south mirrors (MATH.md section 9).

### 4.2 Channels only one satellite relays

A 10 kHz jammer at 20 N 132 E on solo-pac, solo-172e or solo-io reaches the 7, 7 or 5
channels watching that satellite at 51.7 to 55.9 dB-Hz: clearly detected. The tasking
rule finds no partner ("no satellite other than muos-pac relays ... into a channel"), so
there is no position. Only shared-1 and shared-2 can be located.

### 4.3 Sites that cannot see a satellite

Elevation (degrees) over a sidereal day of the illustrative orbits:

| site | MUOS Pacific | 172 E | MUOS Indo-Asia |
|---|---|---|---|
| Honolulu | 52 to 61 | 45 to 50 | -43 to -39 |
| Hilo | 52 to 60 | 44 to 49 | -45 to -41 |
| Tokyo | 25 to 33 | 33 to 40 | 9 to 15 |
| Naha | 20 to 25 | 30 to 35 | 22 to 28 |
| Seoul | 14 to 22 | 23 to 29 | 17 to 25 |
| Busan | 17 to 24 | 26 to 32 | 17 to 24 |
| Manila | 17 to 20 | 28 to 31 | 33 to 37 |

An emitter east of about 160 E is likewise outside the 75 E footprint. It is relayed by
two satellites only, so locating it needs calibration (sections 2.1 and 2.3).

### 4.4 Narrowband (CW) jammers: FDOA only

A 5 dBW CW in shared-2. Predicted with `accuracy_map`: unobservable without calibration
at every point. Calibrated against the Sapporo reference, the 95 % semi-major is 41 /
85 / 25 / 48 / 26 / 27 km (CEP50 7 to 24 km) at the Philippine Sea, South China Sea, Sea
of Japan, near Guam, Korea and east of Japan; near Hawaii it remains unobservable. In
the full chain (uplink-cw scenario, 15 N 130 E, 10 trials each, drawn errors), all 20
pair correlations report FDOA only ("narrowband"). Uncalibrated, 10 of 10 trials are
unobservable. Calibrated, 10 of 10 are located, median miss 18.6 km, 95th percentile
30 km, all inside their ellipses (mean NEES 1.7).

### 4.5 Swept jammers: ambiguous

The uplink-swept scenario (10 kHz sawtooth every 40 ms), 10 trials each: all 20 pair
correlations are "ambiguous", calibrated or not, so there is no position. The system
reports why instead of guessing.

### 4.6 Which sites matter

Predicted 95 % semi-major in km, calibrated / uncalibrated (operator grade, 03 UTC), for
the tasking rule's pairs with one site removed, or with one site only:

| site set | Philippine Sea | South China Sea | Sea of Japan | near Guam | near Hawaii | Korean peninsula | east of Japan |
|---|---|---|---|---|---|---|---|
| all seven | 0.47 / 4.6 | 0.47 / 6.0 | 0.95 / 4.5 | 0.38 / 4.3 | 5.45 / unobs | 0.86 / 4.6 | 0.85 / 4.2 |
| without Honolulu | 0.43 / 5.2 | 0.47 / 6.0 | 0.95 / 5.0 | 0.31 / 4.9 | 5.45 / unobs | 0.85 / 5.1 | 0.86 / 4.6 |
| without Hilo | 0.47 / 4.6 | 0.47 / 6.0 | 0.95 / 4.5 | 0.38 / 4.3 | 5.45 / unobs | 0.86 / 4.6 | 0.85 / 4.2 |
| without Tokyo | 0.47 / 4.6 | 0.47 / 6.0 | 0.95 / 4.5 | 0.38 / 4.3 | 5.45 / unobs | 0.86 / 4.6 | 0.85 / 4.2 |
| without Naha | 0.48 / 4.6 | 0.47 / 6.0 | 0.95 / 4.6 | 0.38 / 4.4 | 5.46 / unobs | 0.86 / 4.6 | 0.86 / 4.3 |
| without Seoul | 0.48 / 4.6 | 0.54 / 5.1 | 0.95 / 4.5 | 0.38 / 4.3 | 5.45 / unobs | 0.86 / 4.6 | 0.85 / 4.2 |
| without Busan | 0.47 / 4.6 | 0.47 / 6.0 | 0.95 / 4.5 | 0.38 / 4.3 | 5.45 / unobs | 0.86 / 4.6 | 0.85 / 4.2 |
| without Manila | 0.47 / 4.6 | 0.47 / 6.0 | 0.95 / 4.5 | 0.38 / 4.3 | 5.45 / unobs | 0.86 / 4.6 | 0.85 / 4.2 |
| only Honolulu | 4.29 / unobs | 8.19 / unobs | 13.4 / unobs | 2.30 / unobs | 5.55 / unobs | 14.2 / unobs | 7.84 / unobs |
| only Hilo | 3.90 / unobs | 7.54 / unobs | 13.2 / unobs | 1.95 / unobs | 5.47 / unobs | 13.9 / unobs | 7.69 / unobs |
| only Tokyo | 0.53 / 5.1 | 0.58 / 5.7 | 0.99 / 5.0 | 0.44 / 4.8 | 5.56 / unobs | 0.91 / 5.1 | 0.90 / 4.6 |
| only Naha | 0.49 / 5.3 | 0.55 / 5.9 | 0.98 / 5.0 | 0.39 / 5.0 | 5.57 / unobs | 0.89 / 5.1 | 0.89 / 4.6 |
| only Seoul | 0.43 / 5.3 | 0.47 / 6.0 | 0.94 / 5.1 | 0.31 / 5.0 | 5.47 / unobs | 0.85 / 5.2 | 0.84 / 4.8 |
| only Busan | 0.50 / 5.3 | 0.56 / 5.9 | 0.98 / 5.1 | 0.40 / 5.0 | 5.59 / unobs | 0.90 / 5.2 | 0.89 / 4.7 |
| only Manila | 0.53 / 5.3 | 0.50 / 6.0 | 1.00 / 5.0 | 0.44 / 5.0 | 5.57 / unobs | 0.92 / 5.1 | 0.90 / 4.6 |

A site with channels on all three satellites forms both pairs on one clock. Its own
clock error then cancels, and the geometry is set by where the satellites are, not by
where the site is. The minimum set for location is therefore one such site; for the
uplink-or-local verdict without a correlation it is two sites more than 200 km apart.
The prediction does not include the co-channel understatement noted in section 3, which
affects single-site pairs most.

## 5. The shipped scenarios and the live system

Through the same chain, the shipped scenarios with their own truth errors were run at
eight epochs 3 h apart, 4 trials each, differing only in noise (`scenarios.json`):

| scenario | emitter | calibration | median miss | largest miss (first candidate) | inside own 95 % ellipse | trials with an unresolved second candidate | 95 % semi-major |
|---|---|---|---|---|---|---|---|
| pacific-demo | jammer-philsea | no | 1.33 km | 1,294 km | 24/32 | 16/32 | 4.6-72 km |
| pacific-demo | jammer-philsea | yes | 0.08 km | 0.44 km | 30/32 | 0/32 | 0.37-0.60 km |
| clock-fault | jammer-philsea | no | 30.8 km | 1,109 km | 2/32 | 12/32 | 4.6-2,145 km |
| clock-fault | jammer-philsea | yes | 0.08 km | 0.44 km | 30/32 | 0/32 | 0.37-0.60 km |
| ephemeris-error | jammer-philsea | no | 25.2 km (smallest 12.0 km) | 1,141 km | 0/32 | 8/32 | 4.4-10,786 km |
| ephemeris-error | jammer-philsea | yes | 0.10 km | 0.54 km | 26/32 | 0/32 | 0.28-0.50 km |
| two-jammers | jammer-a | no | 1.10 km | 1,293 km | 24/32 | 16/32 | 4.6-73 km |
| two-jammers | jammer-a | yes | 0.09 km | 0.23 km | 32/32 | 0/32 | 0.37-0.61 km |
| two-jammers | jammer-b | no | 1.54 km | 3,475 km | 24/32 | 16/32 | 4.4-45 km |
| two-jammers | jammer-b | yes | 0.12 km | 0.62 km | 32/32 | 0/32 | 0.58-0.92 km |

In ephemeris-error, MUOS Pacific is 8 km ahead and 172 E 6 km behind in-track while the
solver assumes 1 km. Calibrated, it is inside at 26 of 32: the part of those errors that
calibration cannot cancel is larger than the solver budgets for.

**Live demo** (`live.json`). `petal-locate demo` was run on 127.0.0.1: the server, seven
node processes and real WebSockets, observed only through the console API, at
2026-11-13 14:20 UTC. That is a different epoch from the tables.
- pacific-demo: verdict uplink 5.0 s and located 7.2 s after detection. After 4, 8, 12
  and 16 accepted rows the fix was 94, 66, 24 and 32 m from the truth, always inside a
  95 % ellipse shrinking from 418 to 379 m by 83 m. Every row was calibrated. The same
  16 rows solved without their reference corrections land 13.3 km off.
- ephemeris-error: located 7.4 s after detection. The fix was 92, 95, 71 and 69 m off,
  inside the ellipse except at 8 rows (95 m against a 349 m by 77 m ellipse); at 16 rows
  the ellipse was 326 m by 77 m. The same rows without corrections land 226 km off
  (NEES 53).

## 6. Reference carriers: why calibration helps now

The old references (13 dBW from a 10 dBi beam aimed at one satellite, 15 dB
front-to-back, 5 kHz) reached the other two satellites through the back of the beam. At
20 N 132 E, `accuracy_map` predicted 3.45 and 1.70 microseconds of reference TDOA noise
on the two demo pairs, and a calibrated 95 % semi-major of 3.5 km against 4.6 km
uncalibrated, with the reference noise dominant. In the chain, calibrated fixes missed
by 1.2 to 2.25 km against 0.6 to 2.6 km uncalibrated (`sim_verify` report).

The new design gives the same Guam and Sapporo sites an omnidirectional antenna (every
satellite gets the full EIRP), 20 dBW and 8 kHz parked at the low edge of each shared
channel. With it:

- C/N0 is 51.2 to 56.2 dB-Hz at all 19 channels, and transponder back-off is 6.2 to 7.2
  dB with the reference alone, 3.3 to 4.0 dB beside the 20 dBW demo jammer (link budget,
  four epochs).
- Predicted reference TDOA noise is 111 and 121 ns. The correlator reports 110 to 160 ns
  (38.4 to 38.9 dB post-correlation SNR), against 90 to 120 ns for the target itself.
- Calibrated fixes are 0.08 km median (section 5), and the predicted ellipse shrinks
  about tenfold (section 2).

8 kHz rather than 5 kHz halves the reference noise (TDOA precision grows as
bandwidth^1.5). More EIRP would not help much and soon hurts. In an ephemeris-dominated
test map near Guam, the calibrated ellipse is at worst 0.15 of the uncalibrated one at
20 or 30 dBW, but 0.38 at 40 dBW, where the reference saturates the transponder and
robs the jammer's power. The reference capture length and repetition in tasking were
left unchanged: one second every `calibration.interval_s` already makes the reference
noise comparable with the target's.

## 7. Caveats

- Everything is simulated, with illustrative link numbers and nominal slots; the
  inclinations and phases are invented. The time-of-day effects depend on them.
- The accuracy map does not model co-channel copies of a signal as noise. On pairs whose
  channel also hears the co-channel satellite, such as seoul-io beside MUOS Pacific, the
  correlator reports up to a third more reference TDOA noise than predicted (155 to 162
  ns against 121 ns). The co-channel same-site TDOA sigma is about 17 % low (section 3).
- The CW prediction is pessimistic: the map assumes the signal fills the correlation
  band, and the correlator measures about 10 dB more post-correlation SNR on a CW
  (`sim_world` report).
- The maps are single-epoch, one capture per pair. The live system adds rows every 5 s,
  so noise averages down while systematic errors stay. With TLE-class errors the
  calibrated ellipse can then shrink past the uncancelled residual (ephemeris-error
  description).
- Detection thresholds assume a noise-like jammer on a quiet channel, with the tracker's
  start-up prior as its baseline (10 s of frames). A jammer under a busy authorised
  carrier is judged against that carrier's power instead.
- No terrain, multipath, interference from other users or real hardware timing was
  modelled. The node drivers for real radios are untested on hardware.

## 8. How the numbers were produced

From `locate/` with the project's virtual environment (Python 3.13, numpy, scipy,
sgp4):

```bash
OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 .venv/bin/python scripts/capability_study.py
OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 .venv/bin/python scripts/capability_study.py --sections live
```

Options: `--sections detection,accuracy,limits,sites,montecarlo,scenarios,live`,
`--trials` (default 40), `--workers` (default 2), `--out` (default `docs/results`).

| file | what | how | time on this machine |
|---|---|---|---|
| `detection.json` | section 1 | `linkbudget.path_budget` for the nominal level; the server's `SignalTracker` on `SimWorld.render_psd` frames, 20 frames at 2 Hz from 4 start times 600 s apart, bisected to 0.25 dB | 2.5 min |
| `accuracy.json` | section 2 | `core.accuracy.accuracy_map` at each point, epochs 03, 09, 15, 21 UTC on 2026-01-01; the coarse grid at 03 UTC; pairs from `sim.montecarlo.plan_event` | 1 min |
| `montecarlo.json` | section 3 | `run_trials(draw_errors=True)`, 40 trials per run, seeds 1000 to 1013 (one per run in table order), t = 2026-01-01T03:00:00Z | 4.5 min (2 workers) |
| `limits.json` | section 4.1 to 4.5 | noise-free truth solves; `plan_event` on solo channels; elevations over a sidereal day; `accuracy_map` for a 5 dBW CW; `run_trials` on uplink-cw and uplink-swept, 10 trials each, seed 31 | 1 min |
| `sites.json` | section 4.6 | the configuration with one site removed or one site kept; `plan_event` and `accuracy_map` at 03 UTC | 0.5 min |
| `scenarios.json` | section 5 | `run_trials(draw_errors=False)` on the shipped scenario files, 4 trials per epoch, 8 epochs 00:00:12 to 21:00:12 UTC, seed 7 | 2.3 min (2 workers) |
| `live.json` | section 5 | `petal-locate demo` on 127.0.0.1, presets loaded through `POST /api/v1/sim/presets/{name}/load`, read through `GET /api/v1/events/{id}` until 16 rows are used | 2 min |

Every section except `live` is deterministic for a given code version and
configuration; `config_sha256_16` in each file records the configuration used. A
second full run reproduced all six deterministic files exactly, apart from timing.
`live.json` carries an earlier hash: it was recorded before comment-only edits to the
YAML. The scenario descriptions in `config/scenarios/*.yaml` quote the server's chain as
re-implemented in `tests/test_sim_verify.py`, at four or eight epochs. That chain picks
the primary by detected SNR and captures references in a later slot. It agrees with
`scenarios.json` within the noise, and its claims are asserted by `test_sim_verify.py`
and, live, by `tests/e2e/test_system.py`.
