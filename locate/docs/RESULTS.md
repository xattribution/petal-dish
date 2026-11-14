# What a PETAL dish network can detect and locate (simulated capability study)

**Every number in this document comes from the PETAL Locate simulator and models, run
on `config/pacific-uhf.yaml`: nominal orbital slots (MUOS Pacific at 177 W, a UFO-class
satellite at 172 E, MUOS Indo-Asia at 75 E, inclinations of 4 to 5 degrees invented),
illustrative link parameters (G/T -14 dB/K, SFD -136 dBW/m^2, 26 dBW saturated EIRP
per 25 kHz channel, 400 K receivers) and generic city-center sites. None of it is
measured on real satellites, terminals or radios.** The simulator and the solver share
the project's geometry code, so these results show what the system does with the
physics as modeled, not that the model matches the sky. Raw numbers are in
`docs/results/*.json`; `scripts/capability_study.py` reproduces them (section 8).

The ionosphere and the troposphere are part of every result. The solver and the
accuracy maps carry them as uncertainty of a stated size, 30 TECU of vertical electron
content (correlation 0.5 between ground ends) and 2.4 m of zenith tropospheric delay
(correlation 0.9), the configured defaults; the Monte Carlo draws a new ionosphere and
troposphere for every trial from those sizes, or applies a fixed level where a row says
so (section 2.4 says what they do).

## The short answer

The question: with small PETAL dishes in Hawaii, Japan, Korea and the Philippines
watching three UHF bent-pipe satellites, what can the network detect and locate, and
how accurately?

**Detection.** On its own, every site detects a noise-like jammer on the uplink of any
satellite it can see. With the 0.72 m PETAL dish the jammer needs about 11 dBW EIRP
toward the satellite if it is 10 kHz wide, or about 15 dBW if it fills a 25 kHz channel
(about 13 W and 32 W into an omnidirectional antenna). A 1.2 m dish detects jammers 3.0
to 3.7 dB weaker (7.2 to 7.8 dBW at 10 kHz), a 10 dBi beam antenna 2.7 to 3.1 dB weaker
(7.7 to 8.5 dBW). Thresholds hardly vary with the site (under 1 dB), because a
bent-pipe satellite relays the whole footprint to every site that sees it. At these
frequencies (260 to 268 MHz downlink, wavelength about 1.15 m) the 0.72 m dish is 0.63
wavelengths across. It behaves as a reflector with 3.3 dBi of gain and a beam about 110
degrees wide, not as a focusing dish, and the configuration check flags every dish
channel for this.

**Location.** A jammer that two or three satellites relay can be located from the
time and frequency differences between its copies. All the figures below include the
ionosphere and the troposphere (see further down). Without calibration, assuming the
satellite positions are known to operator grade (about 1 km), the 95 % error ellipse has
semi-axes of about 5 to 6 km by 0.6 to 0.9 km at 03 UTC. In the Monte Carlo of the
whole chain the median miss is 1.3 to 1.9 km and the 95th percentile 2.9 to 4.9 km.
Calibrating against a reference transmitter at a known place (the configured Guam or
Sapporo references, redesigned in this study so that all three satellites relay them
well) shrinks this about tenfold: semi-axes of 0.5 to 1.0 km by 0.2 to 0.4 km at 03 UTC
(0.3 to 2.0 km over the day). The median miss is then 120 to 320 m and the 95th
percentile 0.27 to 0.94 km. With satellite positions known only to TLE grade (tens of
kilometers in-track), uncalibrated fixes are off by about 23 km (median; 95th
percentile 66 to 73 km). Calibration then depends on distance from the reference: 0.32
km median near Guam (300 km from the reference), 0.88 km in the Philippine Sea (1,500
km away), and a predicted 95 % semi-major of 4.4 to 24 km at about 3,000 km (South China
Sea, Korea, Sea of Japan).

**Where a reference helps.** Moved 650 km from an area of interest, in any direction
from which it still sees all three satellites, the reference kept the TLE-grade 95 %
semi-major to 1.2 to 4.9 km at the six points away from Hawaii, at all four times of day
studied. At 1,300 km the direction matters: 2.3 to 3.3 km due east or west of the area,
6.8 to 10 km due north or south. The reference must see every satellite the pairs use:
placed east of about 160 E, where MUOS Indo-Asia is below its horizon, it leaves those
pairs uncalibrated (up to 25 km). With operator-grade ephemeris the distance matters
little (0.5 to 1.2 km at 650 or 1,300 km).

**The ionosphere and the troposphere.** At these frequencies the ionosphere delays
each radio leg by roughly 0.45 to 1.7 microseconds on a typical day (30 TECU of
electron content, a usual daytime level at low latitudes), the same as 130 to 500 m of
extra path, and the lower atmosphere adds another 8 to 45 ns above 10 degrees
elevation. The system does not try to predict these delays. It treats them
as unknowns of a known typical size and widens every ellipse to cover them.
Calibration cancels the part the jammer and the reference share (the same downlinks to
the same sites) but not the difference between their uplinks, which reach the
satellites at different angles: at 30 TECU about 1.1 microseconds are left on the South
China Sea measurements, against about 0.1 microseconds of measurement noise. The cost
is modest: calibrated ellipses are 6 to 39 % larger than with no ionosphere at all
(0.57 km instead of 0.47 km in the Philippine Sea), and the median calibrated miss is
120 to 320 m instead of 100 to 250 m. In return the fixes stay inside their own
ellipses: 35 to 40 of
40 calibrated fixes at every point with a drawn ionosphere, 36 to 40 of 40 at a fixed 10
or 30 TECU. Without the media terms in the solver, a 30 TECU ionosphere put all 40
calibrated South China Sea fixes outside their ellipses. A day with twice the assumed
level (60 TECU, possible at low latitudes near solar maximum) gives ellipses that are
too small again (20 to 33 of 40 inside), so the assumed level
(`processing.uncertainty.iono_vtec_tecu`) has to match the region and season. In
other bands the ionosphere falls with the square of the frequency (1,600 times smaller
at 12 GHz); the troposphere does not, and with the few-nanosecond measurements of
MHz-wide signals it becomes the larger of the two.

**What the network cannot do:**

- Locate anything on a channel that only one satellite relays (the "solo" channels).
  Every site watching that satellite detects such a jammer (51.7 to 55.9 dB-Hz for
  20 dBW), so the uplink-or-local verdict, which needs sightings at two distant sites,
  still works, but there is no second copy to compare with.
- Locate a CW (unmodulated) jammer without calibration. A CW has no bandwidth, so only
  frequency differences can be measured. Calibrated, a CW fix is coarse: about 16 km
  median miss (95th percentile 50 km), with an ellipse tens of kilometers long.
- Locate a periodically swept jammer, as a rule. The correlations are ambiguous, and the
  system says so instead of giving a position. Only a calibrated pair whose search window
  is narrow enough to hold a single peak of the sweep measures it (seen once live,
  LIMITATIONS.md section 7).
- Locate well near Hawaii. Only two of the three satellites see that area (75 E is
  always below Hawaii's horizon), so there is one pair. It is unobservable without
  calibration, and gives 2.8 to 6.7 km calibrated against the distant Guam reference,
  or 0.6 to 1.8 km with a reference near Honolulu.
- Always choose between two candidate positions when uncalibrated. With satellites
  inclined only 4 to 5 degrees, two hyperbolas can cross twice inside the region. This
  happens at about half the times of day, and the system then shows an unresolved
  ambiguity. Calibration removed it in every simulated case. With zero inclination the
  north-south mirror can never be resolved by the measurements, only by the region.

**Sites.** Location accuracy is set by the satellites, the reference and the
ionosphere, not by the number of sites. Any one of the five Asian sites, each with
channels on all three satellites, predicts within about 20 % of the full seven-site
network, and dropping any one site changes the prediction by at most about 15 %. The
Hawaii sites alone see only two satellites (calibrated 1.9 to 14 km, uncalibrated
unobservable). More sites buy redundancy, an uplink-or-local verdict without a
correlation (it needs sightings at two sites more than 200 km apart), and detection
margin through bigger antennas. They do not buy location accuracy.

## 1. Detection thresholds

The weakest jammer EIRP, in dBW toward the satellite, that the server's spectrum tracker
reports as one detection covering at least 80 % of the jammer's band in at least 3 of 4
independent 10 s looks. The jammer is noise-like, at 20 N 132 E. "10 kHz" is at 301.005
MHz and "25 kHz" fills the 301.000 MHz channel; both are in shared-1, relayed by all
three satellites. Every channel was given the antenna under test, pointed at its
satellite. The beam antenna is linear and carries the configured 3 dB polarization loss;
the dishes carry none. Epoch 2026-01-01 03:00 UTC; reference carriers removed. The media
do not enter detection.

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
4.3). Against the 0.72 m dish the 1.2 m dish gains 3.4 to 3.7 dB at 10 kHz and 3.0 to
3.6 dB at 25 kHz, the beam 2.7 to 3.0 dB and 2.8 to 3.1 dB.

- **Other definitions.** The link-budget "nominal" level, where the jammer's mean
  per-bin excess reaches 4.74 dB (half its bins cross the tracker's 6 dB threshold at
  `navg = 8`), lies 1.1 to 2.0 dB above the table. The tracker joins runs of bins
  separated by gaps of up to 2 bins, so a wide signal is confirmed somewhat below that
  level. 4.7 to 5.9 dB below the nominal level, the tracker already confirms scattered
  detections a few bins wide inside the jammer's band: the operator sees something, but
  not one signal with its true width.
- **Antenna gains.** At the downlink (260.0 to 267.4 MHz), from `core.antennas`: 0.72 m
  dish 3.3 to 3.5 dBi with a half-power beam of 109 to 112 degrees, 1.2 m dish 7.7 to
  7.9 dBi (65 to 67 degrees), beam 10 dBi (52 degrees) less 3 dB of polarization loss.
  Both dishes are under 3 wavelengths (0.63 and 1.05). `SystemConfig.warnings()` gives
  one note per dish channel, 13 in all, "under 3 wavelengths ... behaves as a reflector
  with a broad beam, not as a focusing dish", and the console shows them.
- **Location within the footprint.** Moving the jammer from the sub-satellite point to
  5 degrees elevation adds up to 1.2 dB of uplink path loss.

## 2. Location accuracy over the region

Predicted by `core.accuracy.accuracy_map` (MATH.md section 10): one TDOA and one FDOA
per pair from a single 1 s capture, for the pairs the tasking rule picks for a 20 dBW,
10 kHz noise jammer at the point. The rule picks the primary channel by received C/N0
and one partner per other satellite. Each cell gives the 95 % semi-major axis / CEP50 in
km at 03 UTC; the last column gives the range of the semi-major over 03, 09, 15 and 21
UTC. Elsewhere in this document "an ellipse of a by b" means its 95 % semi-axes. Every
prediction carries the ionosphere (30 TECU, correlation 0.5) and the troposphere (2.4 m,
correlation 0.9) as consider terms; section 2.4 shows the same points with other
assumed sizes. "Calibrated, Guam" uses the configured shared-1 reference. "Nearest ref"
is the same reference design moved, as a what-if, to the nearest of five generic city
centers: Guam, Sapporo, Koror, Taipei, Honolulu.

### 2.1 Operator-grade ephemeris (RIC one-sigma 100 / 1000 / 1000 m, as configured)

| point | satellites seeing it | pairs at 03 UTC | uncalibrated | calibrated, Guam ref | calibrated, nearest ref | dominant error (uncal / cal) | range over the day: uncal / cal Guam / cal nearest |
|---|---|---|---|---|---|---|---|
| Philippine Sea (20 N, 132 E) | 3 | honolulu-pac/naha-172e, honolulu-pac/seoul-io | 5.46 / 1.54 | 0.57 / 0.19 | 0.55 / 0.17 (Taipei, 1,209 km) | ephemeris position / ionosphere | 5.46-61 / 0.47-0.61 / 0.46-0.55 |
| South China Sea (15 N, 115 E) | 3 | seoul-io/seoul-pac, seoul-io/seoul-172e | 6.04 / 1.68 | 0.62 / 0.25 | 0.68 / 0.20 (Taipei, 1,305 km) | ephemeris position / ionosphere | 6.04-201 / 0.58-0.64 / 0.66-0.73 |
| Sea of Japan (40 N, 135 E) | 3 | honolulu-pac/naha-172e, honolulu-pac/seoul-io | 5.27 / 1.49 | 0.99 / 0.30 | 0.51 / 0.16 (Sapporo, 630 km) | ephemeris position / ephemeris position | 5.27-930 / 0.99-2.00 / 0.51-0.93 |
| near Guam (15 N, 147 E) | 3 | honolulu-pac/naha-172e, honolulu-pac/seoul-io | 5.28 / 1.50 | 0.51 / 0.17 | same (Guam, 296 km) | ephemeris position / ionosphere | 5.28-354 / 0.30-0.51 / 0.30-0.51 |
| near Hawaii (20 N, 160 W) | 2 | honolulu-pac/naha-172e (the Seoul pair has no copy) | unobservable | 5.47 / 1.82 | 1.52 / 0.48 (Honolulu, 266 km) | - / ephemeris velocity | unobservable or 11,248-11,953 / 2.80-6.70 / 0.56-1.75 |
| Korean peninsula (37 N, 127.5 E, inland) | 3 | honolulu-pac/naha-172e, honolulu-pac/seoul-io | 5.36 / 1.51 | 0.92 / 0.29 | 0.63 / 0.20 (Sapporo, 1,358 km) | ephemeris position / ephemeris position | 5.36-77 / 0.92-1.66 / 0.63-0.97 |
| Pacific east of Japan (35 N, 150 E) | 3 | honolulu-pac/naha-172e, honolulu-pac/seoul-io | 4.98 / 1.42 | 0.90 / 0.29 | 0.52 / 0.19 (Sapporo, 1,165 km) | ephemeris position / ephemeris position | 4.98-93 / 0.90-1.62 / 0.52-0.98 |

The uncalibrated upper ends (61 to 930 km) come from times of day when the two pairs'
hyperbolas meet at a shallow angle. The ellipse then grows long; in the full chain that
is when a second candidate appears (section 4.1). At 03 UTC the semi-minor axes are 0.6
to 0.9 km uncalibrated and 0.2 to 0.4 km calibrated against Guam (three-satellite
points).

### 2.2 TLE-grade ephemeris (2 / 20 / 10 km, 0.3 / 0.3 / 0.7 m/s, every satellite; the solver assumes the same)

| point | uncalibrated | calibrated, Guam ref | calibrated, nearest ref | distance to the Guam reference | dominant error (cal Guam) | range over the day: uncal / cal Guam / cal nearest |
|---|---|---|---|---|---|---|
| Philippine Sea | 85.0 / 23.8 | 4.42 / 1.26 | 2.88 / 0.84 (Taipei) | 1,538 km | ephemeris velocity | 85-953 / 3.97-5.42 / 2.61-3.97 |
| South China Sea | 110.9 / 30.9 | 5.43 / 1.64 | 5.23 / 1.46 (Taipei) | 3,213 km | ephemeris velocity | 111-3,384 / 4.44-5.43 / 5.23-6.22 |
| Sea of Japan | 77.5 / 21.8 | 9.29 / 2.65 | 2.44 / 0.69 (Sapporo) | 3,090 km | ephemeris position | 78-11,222 / 9.29-24 / 2.44-2.77 |
| near Guam | 80.7 / 22.7 | 1.20 / 0.37 | same (Guam) | 296 km | ephemeris velocity | 81-4,902 / 1.06-1.26 / 1.06-1.26 |
| near Hawaii | unobservable | 81.9 / 29.0 | 4.73 / 1.48 (Honolulu) | 5,911 km | ephemeris velocity | unobservable or 11,250-11,954 / 54-99 / 1.85-5.56 |
| Korean peninsula | 80.3 / 22.5 | 8.59 / 2.46 | 4.51 / 1.27 (Sapporo) | 3,120 km | ephemeris position | 80-1,131 / 8.59-21 / 4.51-4.95 |
| Pacific east of Japan | 72.4 / 20.5 | 9.89 / 2.78 | 2.91 / 0.86 (Sapporo) | 2,442 km | ephemeris position | 72-1,343 / 9.89-19 / 2.91-6.60 |

With TLE-grade ephemeris, calibration removes the part of the satellite error that the
jammer and the reference share. What is left grows with the distance between them, and
the geometry matters too: Taipei is nearer the South China Sea point than Guam but helps
no more (section 2.5). **Dominant error source:** uncalibrated, it is the satellite
position at every point and both grades. Calibrated with operator-grade ephemeris it is
the ionosphere in the Philippine Sea, the South China Sea and near Guam, and the
uncancelled ephemeris position farther north (Sea of Japan, Korea, east of Japan); with
TLE-grade ephemeris it is the ephemeris velocity or position everywhere (the ionosphere
changes those ellipses by under 3 %, section 2.4).

### 2.3 Where a shared-channel jammer can be located at all (03 UTC, 95 % semi-major in km)

Each cell holds the prediction for the tasking rule's pairs. "unobs" means fewer than
two independent rows constrain the position: one satellite pair, whose uncalibrated FDOA
carries the oscillator offsets. Longitudes 160 E to 160 W are seen only by MUOS Pacific
and 172 E.

Operator-grade, uncalibrated:

| lat \ lon | 100 | 110 | 120 | 130 | 140 | 150 | 160 | 170 | 180 | -170 | -160 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| -10 | >100 | 9.5 | 9.0 | 8.7 | 8.4 | 8.3 | unobs | unobs | unobs | unobs | unobs |
| 0 | >100 | 7.5 | 7.2 | 6.9 | 6.6 | 6.4 | unobs | unobs | unobs | unobs | unobs |
| 10 | >100 | 6.5 | 6.2 | 6.0 | 5.7 | 5.5 | unobs | unobs | unobs | unobs | unobs |
| 20 | >100 | 5.9 | 5.7 | 5.5 | 5.3 | 5.0 | unobs | unobs | unobs | unobs | unobs |
| 30 | >100 | 5.6 | 5.5 | 5.3 | 5.1 | 4.9 | unobs | unobs | unobs | unobs | unobs |
| 40 | >100 | 5.6 | 5.5 | 5.3 | 5.2 | 5.1 | unobs | unobs | unobs | unobs | unobs |
| 50 | unobs | 5.8 | 5.7 | 5.6 | 5.5 | 5.4 | unobs | unobs | unobs | unobs | unobs |

Operator-grade, calibrated against the Guam reference:

| lat \ lon | 100 | 110 | 120 | 130 | 140 | 150 | 160 | 170 | 180 | -170 | -160 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| -10 | 1.5 | 1.3 | 1.2 | 1.2 | 1.2 | 1.2 | 5.6 | 5.8 | 6.3 | 7.1 | 8.3 |
| 0 | 1.0 | 0.9 | 0.8 | 0.8 | 0.7 | 0.8 | 3.6 | 3.9 | 4.5 | 5.3 | 6.4 |
| 10 | 0.8 | 0.7 | 0.6 | 0.5 | 0.5 | 0.5 | 2.2 | 2.8 | 3.5 | 4.4 | 5.5 |
| 20 | 0.9 | 0.7 | 0.6 | 0.6 | 0.6 | 0.6 | 2.9 | 3.3 | 3.9 | 4.7 | 5.5 |
| 30 | 1.3 | 0.9 | 0.8 | 0.8 | 0.7 | 0.8 | 5.4 | 5.4 | 5.6 | 6.0 | 6.5 |
| 40 | 2.1 | 1.1 | 1.0 | 1.0 | 1.0 | 1.1 | 9.0 | 8.5 | 8.3 | 8.3 | 8.5 |
| 50 | unobs | 1.3 | 1.3 | 1.3 | 1.3 | 1.4 | 14.4 | 13.2 | 12.5 | 12.2 | 12.2 |

TLE-grade, calibrated against the Guam reference:

| lat \ lon | 100 | 110 | 120 | 130 | 140 | 150 | 160 | 170 | 180 | -170 | -160 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| -10 | 19.7 | 16.4 | 15.7 | 14.7 | 13.2 | 13.2 | 75.8 | 80.2 | 88.6 | >100 | >100 |
| 0 | 13.0 | 10.3 | 9.6 | 8.8 | 7.2 | 6.7 | 47.2 | 53.5 | 63.8 | 77.6 | 95.4 |
| 10 | 9.0 | 6.2 | 4.7 | 3.4 | 2.4 | 2.3 | 24.2 | 36.9 | 50.6 | 65.6 | 82.8 |
| 20 | 10.2 | 7.5 | 6.1 | 4.7 | 2.9 | 4.0 | 34.7 | 44.4 | 55.7 | 68.1 | 81.9 |
| 30 | 16.8 | 10.0 | 8.6 | 6.9 | 6.0 | 7.9 | 72.0 | 73.8 | 78.5 | 85.0 | 92.9 |
| 40 | 27.6 | 10.9 | 9.9 | 9.2 | 9.8 | 12.0 | >100 | >100 | >100 | >100 | >100 |
| 50 | unobs | 12.7 | 12.5 | 13.0 | 14.4 | 16.9 | >100 | >100 | >100 | >100 | >100 |

### 2.4 The ionosphere and the troposphere

**Size per leg.** One-way delay of a single leg (ns) against the elevation of the
satellite seen from the leg's ground end (thin-shell ionosphere at 350 km; Black and
Eisner tropospheric mapping; MATH.md section 3):

| elevation | 90 deg | 30 deg | 10 deg | 5 deg |
|---|---|---|---|---|
| ionosphere, 10 TECU, uplink 301.005 MHz | 148 | 260 | 414 | 451 |
| ionosphere, 30 TECU, uplink 301.005 MHz | 445 | 779 | 1,242 | 1,353 |
| ionosphere, 10 TECU, downlink 260.0 MHz | 199 | 348 | 555 | 604 |
| ionosphere, 30 TECU, downlink 260.0 MHz | 597 | 1,045 | 1,664 | 1,813 |
| troposphere, 2.4 m zenith | 8 | 16 | 45 | 82 |

**What the rows carry.** The TDOA each pair's row would carry at 03 UTC from 30 TECU and
2.4 m at every ground end (ns), and what is left after differencing against the Guam
reference measured on the same pair (`core.media`, the solver's own partials). Near
Hawaii only the pair with a copy is listed.

| point | pair | ionosphere | ionosphere, calibrated | troposphere | troposphere, calibrated |
|---|---|---|---|---|---|
| Philippine Sea | honolulu-pac/naha-172e | -220 | 35 | -4 | 1 |
| Philippine Sea | honolulu-pac/seoul-io | -768 | 464 | -18 | 24 |
| South China Sea | seoul-io/seoul-pac | -472 | -1,111 | -19 | -50 |
| South China Sea | seoul-io/seoul-172e | -139 | -954 | -6 | -39 |
| Sea of Japan | honolulu-pac/naha-172e | -305 | -49 | -6 | -1 |
| Sea of Japan | honolulu-pac/seoul-io | -919 | 313 | -29 | 14 |
| near Guam | honolulu-pac/naha-172e | -265 | -10 | -6 | 0 |
| near Guam | honolulu-pac/seoul-io | -1,310 | -78 | -53 | -11 |
| near Hawaii | honolulu-pac/naha-172e | -396 | -141 | -9 | -3 |
| Korean peninsula | honolulu-pac/naha-172e | -267 | -12 | -5 | 0 |
| Korean peninsula | honolulu-pac/seoul-io | -649 | 583 | -15 | 28 |
| Pacific east of Japan | honolulu-pac/naha-172e | -322 | -66 | -7 | -2 |
| Pacific east of Japan | honolulu-pac/seoul-io | -1,352 | -120 | -110 | -68 |

Calibration cancels the downlinks (same sites, frequencies 12.5 kHz apart) but not the
uplinks, because the emitter and the reference see the satellites at different
elevations. Near the reference (near Guam) little is left; 3,000 km away (South China
Sea) the calibrated rows keep about 1 microsecond, ten times their CAF noise of about
100 ns. The troposphere is 2 to 8 % of a 30 TECU ionosphere here.

**What it costs.** The 03 UTC prediction of section 2.1 with the solver's assumed media
set to other sizes (95 % semi-major in km; VTEC / zenith delay):

| map | point | none (0 / 0) | troposphere only (0 / 2.4 m) | 10 TECU / 2.4 m | 30 TECU / 2.4 m (configured) | 60 TECU / 2.4 m | dominant (configured) |
|---|---|---|---|---|---|---|---|
| operator, uncalibrated | Philippine Sea | 4.59 | 4.59 | 4.69 | 5.46 | 7.50 | ephemeris position |
| operator, uncalibrated | South China Sea | 5.95 | 5.95 | 5.96 | 6.04 | 6.29 | ephemeris position |
| operator, uncalibrated | Sea of Japan | 4.47 | 4.47 | 4.57 | 5.27 | 7.15 | ephemeris position |
| operator, uncalibrated | near Guam | 4.34 | 4.34 | 4.45 | 5.28 | 7.41 | ephemeris position |
| operator, uncalibrated | Korean peninsula | 4.56 | 4.56 | 4.65 | 5.36 | 7.25 | ephemeris position |
| operator, uncalibrated | Pacific east of Japan | 4.17 | 4.17 | 4.27 | 4.98 | 6.85 | ephemeris position |
| operator, calibrated Guam | Philippine Sea | 0.47 | 0.47 | 0.48 | 0.57 | 0.83 | ionosphere |
| operator, calibrated Guam | South China Sea | 0.55 | 0.55 | 0.56 | 0.62 | 0.78 | ionosphere |
| operator, calibrated Guam | Sea of Japan | 0.94 | 0.94 | 0.95 | 0.99 | 1.15 | ephemeris position |
| operator, calibrated Guam | near Guam | 0.37 | 0.37 | 0.38 | 0.51 | 0.78 | ionosphere |
| operator, calibrated Guam | near Hawaii | 5.45 | 5.45 | 5.45 | 5.47 | 5.53 | ephemeris velocity |
| operator, calibrated Guam | Korean peninsula | 0.86 | 0.86 | 0.86 | 0.92 | 1.09 | ephemeris position |
| operator, calibrated Guam | Pacific east of Japan | 0.85 | 0.86 | 0.86 | 0.90 | 1.07 | ephemeris position |
| TLE, calibrated Guam | Philippine Sea | 4.41 | 4.41 | 4.41 | 4.42 | 4.45 | ephemeris velocity |
| TLE, calibrated Guam | South China Sea | 5.42 | 5.42 | 5.42 | 5.43 | 5.45 | ephemeris velocity |
| TLE, calibrated Guam | Sea of Japan | 9.28 | 9.28 | 9.29 | 9.29 | 9.30 | ephemeris position |
| TLE, calibrated Guam | near Guam | 1.18 | 1.18 | 1.18 | 1.20 | 1.31 | ephemeris velocity |
| TLE, calibrated Guam | near Hawaii | 81.9 | 81.9 | 81.9 | 81.9 | 81.9 | ephemeris velocity |
| TLE, calibrated Guam | Korean peninsula | 8.58 | 8.58 | 8.58 | 8.59 | 8.61 | ephemeris position |
| TLE, calibrated Guam | Pacific east of Japan | 9.88 | 9.88 | 9.88 | 9.89 | 9.90 | ephemeris position |

The configured 30 TECU widens the operator-grade ellipses by 1 to 22 % uncalibrated and
by 6 to 39 % calibrated; with TLE-grade ephemeris the satellite errors dwarf it (under
3 %). The troposphere alone changes nothing visible at these bandwidths. The Monte Carlo
(section 3) checks that these wider ellipses are the right size.

### 2.5 Where to put a reference

The configured Guam reference design moved 650 or 1,300 km from each point at eight
bearings; the largest calibrated 95 % semi-major (km) over the four epochs, TLE-grade
ephemeris. A star marks a placement from which the reference could not calibrate every
pair at every epoch (it does not see a satellite the pairs use). Near Hawaii only one
pair has a copy.

| point | distance km | N | NE | E | SE | S | SW | W | NW |
|---|---|---|---|---|---|---|---|---|---|
| Philippine Sea | 650 | 3.7 | 2.9 | 1.3 | 3.0 | 3.8 | 3.0 | 1.3 | 2.8 |
| Philippine Sea | 1,300 | 7.1 | 5.6 | 2.4 | 6.1 | 7.5 | 6.1 | 2.4 | 5.2 |
| South China Sea | 650 | 3.5 | 2.7 | 1.2 | 2.7 | 3.5 | 2.8 | 1.2 | 2.5 |
| South China Sea | 1,300 | 6.8 | 5.3 | 2.3 | 5.3 | 7.0 | 5.5 | 2.5* | 4.6 |
| Sea of Japan | 650 | 4.5 | 3.3 | 1.9 | 3.8 | 4.9 | 3.6 | 1.9 | 3.5 |
| Sea of Japan | 1,300 | 8.2 | 9.2* | 8.6* | 7.7 | 9.9 | 7.4 | 3.3 | 6.4 |
| near Guam | 650 | 3.5 | 2.9 | 1.3 | 3.1 | 3.6 | 3.0 | 1.3 | 3.0 |
| near Guam | 1,300 | 6.8 | 19.9* | 12.7* | 6.3 | 7.1 | 6.0 | 2.3 | 5.7 |
| near Hawaii | 650 | 20.6 | 14.9 | 10.0 | 17.9 | 21.8 | 16.3 | 10.5 | 17.3 |
| near Hawaii | 1,300 | 39.7 | 28.2 | 19.1 | 36.1 | 44.4 | 33.6 | 21.2 | 33.6 |
| Korean peninsula | 650 | 4.3 | 3.2 | 1.8 | 3.6 | 4.6 | 3.5 | 1.7 | 3.3 |
| Korean peninsula | 1,300 | 8.0 | 5.8 | 3.1 | 7.3 | 9.5 | 7.1 | 3.2 | 6.1 |
| Pacific east of Japan | 650 | 4.5* | 13.1* | 9.4* | 11.0* | 4.9 | 3.7 | 1.8 | 3.6 |
| Pacific east of Japan | 1,300 | 8.6* | 24.7* | 16.0* | 24.7* | 10.0 | 7.5 | 3.2 | 6.6 |

East-west placements help most, north-south placements least. With operator-grade
ephemeris the same placements give 0.5 to 1.0 km at 650 km and 0.5 to 1.2 km at 1,300
km at the six points away from Hawaii (up to 2.3 km where starred): there the satellite
errors are small, and the ionosphere, the reference's own noise and the geometry set the
ellipse.

## 3. Monte Carlo confirmation (full chain)

`sim.montecarlo.run_trials` with `draw_errors=True`: simulated IQ, then the CAF
estimator, the reference calibration and the solver. Every trial draws new satellite
ephemeris, oscillator, site-clock and per-pair TDOA-bias errors from the configured
one-sigma values, and one world-wide VTEC |N(0, 30 TECU)| and zenith delay
|N(0, 2.4 m)| (drawn VTECs ran from 0.2 to 103 TECU); the solver assumes the same
values. 40 trials per row, 1 s captures, 03 UTC, pairs by the tasking rule. "Inside" is
the fraction of fixes inside the solver's own 95 % ellipse; the ideal mean NEES is 2.
With 40 trials the binomial standard error of a 95 % containment is 0.034, and the
standard error of a mean NEES of 2 is 0.32.

| point | ephemeris | calibration | median miss | 95th pct miss | inside own 95 % ellipse | mean NEES | predicted CEP50 |
|---|---|---|---|---|---|---|---|
| Philippine Sea | operator | none | 1.64 km | 3.06 km | 1.000 (40/40) | 1.51 | 1.54 km |
| Philippine Sea | operator | Guam ref | 0.20 km | 0.45 km | 0.950 (38/40) | 2.37 | 0.19 km |
| South China Sea | operator | none | 1.87 km | 4.89 km | 0.925 (37/40) | 2.23 | 1.68 km |
| South China Sea | operator | Guam ref | 0.24 km | 0.53 km | 0.875 (35/40) | 2.34 | 0.24 km |
| Sea of Japan | operator | none | 1.29 km | 4.33 km | 1.000 (40/40) | 1.51 | 1.49 km |
| Sea of Japan | operator | Guam ref | 0.32 km | 0.94 km | 0.925 (37/40) | 2.48 | 0.30 km |
| near Guam | operator | none | 1.38 km | 2.95 km | 1.000 (40/40) | 1.12 | 1.50 km |
| near Guam | operator | Guam ref | 0.12 km | 0.27 km | 0.975 (39/40) | 1.18 | 0.17 km |
| east of Japan | operator | none | 1.29 km | 3.31 km | 0.950 (38/40) | 1.65 | 1.42 km |
| east of Japan | operator | Guam ref | 0.24 km | 0.71 km | 1.000 (40/40) | 1.00 | 0.29 km |
| Philippine Sea | TLE | none | 23.15 km | 65.68 km | 0.975 (39/40) | 1.76 | 23.79 km |
| Philippine Sea | TLE | Guam ref | 0.88 km | 3.04 km | 0.975 (39/40) | 1.52 | 1.26 km |
| near Guam | TLE | none | 23.43 km | 72.54 km | 0.925 (37/40) | 2.62 | 22.73 km |
| near Guam | TLE | Guam ref | 0.32 km | 1.11 km | 0.975 (39/40) | 1.52 | 0.36 km |

The predicted CEP50 agrees with the measured median miss within 25 % in 12 of the 14
rows; the median of 40 misses itself scatters by about 18 %. The two exceptions are
cautious: calibrated near Guam (0.12 km measured, 0.17 km predicted) and TLE-grade
calibrated Philippine Sea (0.88 km against 1.26 km). None of the 03 UTC trials showed a
second candidate.

**Ionosphere and troposphere, calibrated (Guam reference), operator grade.** The same
seeds as the rows above, so within a point only the media differ: "off" has no media in
the simulation and none in the solver (the model before the media terms); "drawn" is the
table above; "fixed" applies one level everywhere (10, 30 or 60 TECU, with 2.3 m of
troposphere); "not modeled" applies 30 TECU and 2.3 m to a solver without media terms.
Each cell: median miss in km / inside own 95 % ellipse (of 40) / mean NEES.

| point | off | drawn | fixed 10 TECU | fixed 30 TECU | fixed 60 TECU | 30 TECU, not modeled |
|---|---|---|---|---|---|---|
| Philippine Sea | 0.13 / 36 / 2.23 | 0.20 / 38 / 2.37 | 0.14 / 40 / 1.01 | 0.16 / 40 / 1.41 | 0.23 / 33 / 4.07 | 0.15 / 17 / 7.52 |
| South China Sea | 0.16 / 36 / 2.70 | 0.24 / 35 / 2.34 | 0.18 / 40 / 0.93 | 0.29 / 39 / 2.71 | 0.42 / 20 / 6.19 | 0.29 / 0 / 24.85 |
| Sea of Japan | 0.25 / 37 / 2.77 | 0.32 / 37 / 2.48 | 0.31 / 36 / 2.36 | 0.31 / 36 / 2.49 | - | 0.28 / 30 / 4.12 |
| near Guam | 0.10 / 38 / 2.08 | 0.12 / 39 / 1.18 | 0.13 / 39 / 1.06 | 0.13 / 39 / 1.23 | - | 0.11 / 35 / 2.39 |
| east of Japan | 0.25 / 38 / 1.93 | 0.24 / 40 / 1.00 | 0.23 / 40 / 0.93 | 0.27 / 40 / 1.12 | - | 0.27 / 34 / 2.53 |

- With the media terms, fixes stay inside their ellipses at 10 and 30 TECU (36 to 40 of
  40). The mean errors show what the uncancelled uplink delay does: at 30 TECU the
  calibrated South China Sea fixes sit 203 m west of the truth on average (standard
  error 9 m), the Philippine Sea fixes 89 m west, and the ellipses are wide enough for
  it.
- Without them, a 30 TECU ionosphere puts every calibrated South China Sea fix outside
  its ellipse (mean NEES 25) and more than half of the Philippine Sea fixes: the
  reported ellipse would be wrong, not just large.
- At 60 TECU, twice the assumed sigma, 33 and 20 of 40 are inside (mean NEES 4.1 and
  6.2), and the South China Sea fixes sit 377 m west on average: above the configured
  size the consider term no longer covers the delay.
- Uncalibrated, the media barely matter against the satellite errors: with drawn, fixed
  10 TECU and fixed 30 TECU media the uncalibrated rows keep 37 to 40 of 40 inside and
  median misses of 1.2 to 2.1 km (off: 36 to 39 of 40, 1.2 to 2.1 km).
- The calibrated South China Sea row stays slightly over-confident without media (36 of
  40, mean NEES 2.70, two standard errors above 2); its pairs are formed at Seoul with
  co-channel satellites.

**The correlator's reported sigmas** (`limits.json`, `caf_sigma_check`): 100 noise
realizations at 20 N 132 E with no truth errors, the scatter of the error over the
reported sigma (1 is ideal; standard error 0.07). On the co-channel pair at one site
(seoul-pac / seoul-io), where both channels hear both satellites, it is 1.02 for TDOA
and 1.02 for FDOA (mean reported TDOA sigma 144 ns); on a cross-site pair of different
satellites (honolulu-pac / naha-172e) 0.90 and 1.00 (92 ns). The co-channel same-site
TDOA sigma, about 17 % low in earlier versions, now carries the coherent self-peak term
of MATH.md section 6.

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

A 5 dBW CW in shared-2. Predicted with `accuracy_map` (which band-limits a CW to 10 Hz
plus the Doppler reach, as the correlator does): unobservable without calibration at
every point. Calibrated against the Sapporo reference, the 95 % semi-major is 44 / 74 /
27 / 53 / 29 / 31 km (CEP50 7.7 to 20 km) at the Philippine Sea, South China Sea, Sea
of Japan, near Guam, Korea and east of Japan; near Hawaii it remains unobservable. In
the full chain (uplink-cw scenario, 15 N 130 E, 40 trials each, drawn errors and media),
all 80 pair correlations report FDOA only ("narrowband"). Uncalibrated, 40 of 40 trials
are unobservable. Calibrated, 40 of 40 are located: median miss 15.5 km against a
predicted CEP50 of 15.0 km, 95th percentile 50 km, 35 of 40 inside their ellipses (mean
NEES 2.1). Each of these is one round's fit from two FDOA rows, with no redundancy: the
server shows such a position but does not count it as a fix (MATH.md section 9, fix
gate) and locates a CW only once later rounds add spare rows.

### 4.5 Swept jammers: ambiguous

The uplink-swept scenario (10 kHz sawtooth every 40 ms), 40 trials each: all 80 pair
correlations are "ambiguous", calibrated or not, so there is no position. The system
reports why instead of guessing. A pair is ambiguous when its search window holds more
than one of the sweep's correlation peaks; a calibrated window narrow enough to hold
one gives a valid measurement (LIMITATIONS.md section 7).

### 4.6 Which sites matter

Predicted 95 % semi-major in km, calibrated / uncalibrated (operator grade, 03 UTC), for
the tasking rule's pairs with one site removed, or with one site only:

| site set | Philippine Sea | South China Sea | Sea of Japan | near Guam | near Hawaii | Korean peninsula | east of Japan |
|---|---|---|---|---|---|---|---|
| all seven | 0.57 / 5.5 | 0.62 / 6.0 | 0.99 / 5.3 | 0.51 / 5.3 | 5.47 / unobs | 0.92 / 5.4 | 0.90 / 5.0 |
| without Honolulu | 0.59 / 6.2 | 0.62 / 6.0 | 1.03 / 6.0 | 0.52 / 6.0 | 5.52 / unobs | 0.95 / 6.0 | 0.93 / 5.7 |
| without Hilo | 0.57 / 5.5 | 0.62 / 6.0 | 0.99 / 5.3 | 0.51 / 5.3 | 5.47 / unobs | 0.92 / 5.4 | 0.90 / 5.0 |
| without Tokyo | 0.57 / 5.5 | 0.62 / 6.0 | 0.99 / 5.3 | 0.51 / 5.3 | 5.47 / unobs | 0.92 / 5.4 | 0.90 / 5.0 |
| without Naha | 0.57 / 5.6 | 0.62 / 6.0 | 1.00 / 5.5 | 0.51 / 5.4 | 5.48 / unobs | 0.92 / 5.5 | 0.90 / 5.1 |
| without Seoul | 0.55 / 5.5 | 0.58 / 6.0 | 0.99 / 5.3 | 0.49 / 5.3 | 5.47 / unobs | 0.91 / 5.4 | 0.90 / 5.0 |
| without Busan | 0.57 / 5.5 | 0.62 / 6.0 | 0.99 / 5.3 | 0.51 / 5.3 | 5.47 / unobs | 0.92 / 5.4 | 0.90 / 5.0 |
| without Manila | 0.57 / 5.5 | 0.62 / 6.0 | 0.99 / 5.3 | 0.51 / 5.3 | 5.47 / unobs | 0.92 / 5.4 | 0.90 / 5.0 |
| only Honolulu | 4.29 / unobs | 8.36 / unobs | 13.5 / unobs | 2.30 / unobs | 5.57 / unobs | 14.3 / unobs | 7.89 / unobs |
| only Hilo | 3.91 / unobs | 7.73 / unobs | 13.3 / unobs | 1.95 / unobs | 5.49 / unobs | 14.0 / unobs | 7.73 / unobs |
| only Tokyo | 0.65 / 5.2 | 0.68 / 5.8 | 1.07 / 5.0 | 0.59 / 4.9 | 5.63 / unobs | 1.00 / 5.1 | 0.98 / 4.7 |
| only Naha | 0.66 / 5.4 | 0.69 / 6.1 | 1.09 / 5.1 | 0.59 / 5.0 | 5.67 / unobs | 1.02 / 5.2 | 0.99 / 4.7 |
| only Seoul | 0.58 / 5.4 | 0.62 / 6.0 | 1.01 / 5.1 | 0.52 / 5.1 | 5.51 / unobs | 0.94 / 5.3 | 0.92 / 4.8 |
| only Busan | 0.67 / 5.3 | 0.70 / 6.0 | 1.10 / 5.1 | 0.60 / 5.0 | 5.69 / unobs | 1.02 / 5.2 | 1.00 / 4.8 |
| only Manila | 0.65 / 5.5 | 0.63 / 6.2 | 1.08 / 5.0 | 0.58 / 5.1 | 5.65 / unobs | 1.01 / 5.2 | 0.98 / 4.7 |

A site with channels on all three satellites forms both pairs on one clock. Its own
clock error then cancels, and the geometry is set by where the satellites are, not by
where the site is. The minimum set for location is therefore one such site; for the
uplink-or-local verdict without a correlation it is two sites more than 200 km apart.
One Asian site alone predicts at most 17 % above the full network (Busan near Guam),
and dropping one site changes a prediction by at most 14 % (Honolulu, uncalibrated).

## 5. The shipped scenarios and the live system

Through the same chain, the shipped scenarios with their own truth errors were run at
eight epochs 3 h apart, 4 trials each, differing only in noise (`scenarios.json`). The
shipped scenarios have no ionosphere or troposphere, but the solver carries both at
the configured sizes, so their calibrated ellipses are cautious; "pacific-demo + media"
adds a 30 TECU ionosphere and 2.3 m of troposphere to pacific-demo.

| scenario | emitter | calibration | median miss | largest miss (first candidate) | inside own 95 % ellipse | trials with an unresolved second candidate | 95 % semi-major |
|---|---|---|---|---|---|---|---|
| pacific-demo | jammer-philsea | no | 1.33 km | 1,294 km | 24/32 | 16/32 | 5.5-85 km |
| pacific-demo | jammer-philsea | yes | 0.13 km | 0.44 km | 32/32 | 0/32 | 0.47-0.61 km |
| pacific-demo + media | jammer-philsea | no | 1.26 km | 1,294 km | 24/32 | 16/32 | 5.5-85 km |
| pacific-demo + media | jammer-philsea | yes | 0.13 km | 0.38 km | 32/32 | 0/32 | 0.47-0.61 km |
| clock-fault | jammer-philsea | no | 30.8 km | 1,109 km | 4/32 | 12/32 | 5.5-1,956 km |
| clock-fault | jammer-philsea | yes | 0.13 km | 0.43 km | 32/32 | 0/32 | 0.47-0.61 km |
| ephemeris-error | jammer-philsea | no | 25.2 km (smallest 12.0 km) | 1,141 km | 0/32 | 8/32 | 5.4-10,739 km |
| ephemeris-error | jammer-philsea | yes | 0.13 km | 0.50 km | 30/32 | 0/32 | 0.34-0.51 km |
| two-jammers | jammer-a | no | 1.10 km | 1,293 km | 24/32 | 16/32 | 5.5-85 km |
| two-jammers | jammer-a | yes | 0.08 km | 0.24 km | 32/32 | 0/32 | 0.47-0.61 km |
| two-jammers | jammer-b | no | 1.54 km | 3,475 km | 24/32 | 16/32 | 5.2-53 km |
| two-jammers | jammer-b | yes | 0.10 km | 0.59 km | 32/32 | 0/32 | 0.72-0.95 km |

In ephemeris-error, MUOS Pacific is 8 km ahead and 172 E 6 km behind in-track while the
solver assumes 1 km. Calibrated, it is inside at 30 of 32: the part of those errors
that calibration cannot cancel is larger than the solver budgets for, and the media
allowance, unused in this scenario, hides part of it. Calibrated, the reference TDOA
noise the correlator measured was 107 to 120 ns on pairs without a co-channel copy and
152 to 164 ns on pairs through seoul-io.

**Live demo** (`live.json`). `petal-locate demo --start 2026-01-01T03:00:00Z` was run on
127.0.0.1: the server, seven node processes and real WebSockets, observed only through
the console API, with simulated time starting at the main epoch of the tables (the
moments at which rounds ran still followed the wall clock).

- pacific-demo: verdict uplink 5.0 s and located 7.4 s after detection. After 4, 8, 12
  and 16 accepted rows the fix was 353, 252, 237 and 227 m from the truth, always inside
  a 95 % ellipse shrinking from 560 by 246 m to 496 by 231 m, with the ionosphere the
  dominant term (the preset has none; the server's solver allows for one). Every row was
  calibrated. The same 16 rows solved without their reference corrections land 0.42 km
  off, inside their 5.4 km ellipse: this preset's satellite errors are within the
  operator-grade sigmas.
- ephemeris-error: verdict uplink 5.0 s and located 7.4 s after detection. The fix was
  88, 15, 18 and 61 m off, always inside; at 16 rows the ellipse was 437 by 164 m. The
  same rows without corrections land 12.0 km off (NEES 31). This run predates the
  ephemeris allowance: with many more rows the fix stayed a couple of hundred meters off
  while its ellipse shrank past that. The server now widens the position sigma of the
  satellites the clock checks put wrong (MATH.md section 9), and a first fix waits for
  that; live, the ellipse then kept the truth in every snapshot (LIMITATIONS.md section
  7).

## 6. Reference carriers: why calibration helps now

The old references (13 dBW from a 10 dBi beam aimed at one satellite, 15 dB
front-to-back, 5 kHz) reached the other two satellites through the back of the beam. At
20 N 132 E, `accuracy_map` predicts 3.45 and 1.70 microseconds of reference TDOA noise
on the two demo pairs with that design, and a calibrated 95 % semi-major of 2.93 km
against 5.46 km uncalibrated, with the reference noise dominant (`references.json`).

The new design gives the same Guam and Sapporo sites an omnidirectional antenna (every
satellite gets the full EIRP), 20 dBW and 8 kHz parked at the low edge of each shared
channel. With it:

- C/N0 is 51.2 to 56.2 dB-Hz at all 19 channels, and transponder back-off is 6.2 to 7.2
  dB with the reference alone, 3.3 to 4.0 dB beside a 20 dBW jammer (link budget, four
  epochs).
- Predicted reference TDOA noise is 111 and 159 ns on the two pairs; the second goes
  through seoul-io, which also hears MUOS Pacific's co-channel copy of the reference.
  The correlator reports 107 to 120 and 152 to 164 ns (section 5), against 90 to 145 ns
  for the target itself (section 3).
- Calibrated fixes are 0.08 to 0.13 km median (section 5), and the predicted ellipse
  shrinks about tenfold (5.46 to 0.57 km at 20 N 132 E).

At the same 20 dBW, 8 kHz rather than 5 kHz cuts the reference TDOA noise by 1.6 to 1.8
(178 and 284 ns at 5 kHz against 111 and 159 ns). At fixed EIRP and high in-band SNR the
noise falls as 1 / bandwidth; it would fall as bandwidth^-1.5 only at equal spectral
density (5 kHz at 18.0 dBW: 226 and 322 ns, about 2.0 times), and as bandwidth^-0.5 at
low SNR. On the second pair a co-channel copy of the reference, which grows with it,
limits the SNR, so the gain there moves toward the bandwidth^1.5 law. More EIRP would
not help much and soon hurts. In a map near Guam (10 to 20 N, 140 to 150 E, operator
grade), the calibrated ellipse is at worst 0.11 of the uncalibrated one at 20 or 30 dBW,
but 0.20 at 40 dBW (median calibrated semi-major 0.52 km at 20 dBW, 1.03 km at 40 dBW),
where the reference saturates the transponder by 13 dB (back-off -12.7 to -13.7 dB) and
robs the jammer's power; at 30 dBW it is already 2.7 to 3.7 dB into saturation without
visible harm. The reference capture length and repetition in tasking were left
unchanged: one second every
`calibration.interval_s` already makes the reference noise comparable with the
target's.

## 7. Caveats

- Everything is simulated, with illustrative link numbers and nominal slots; the
  inclinations and phases are invented. The time-of-day effects depend on them.
- The ionosphere and troposphere are modeled simply: a thin shell at 350 km and the
  Black and Eisner mapping, one VTEC and one zenith delay per ground end, no gradients
  along a path, no scintillation. The simulator applies one value at every ground end,
  so the Monte Carlo tests only the fully correlated case of the solver's per-end
  model. The configured 30 TECU is a daytime low-latitude level; above the configured
  size the ellipses are too small (section 3, 60 TECU), and no TEC map or broadcast
  model is applied to reduce the delay before the solver sees it.
- The accuracy map's term for co-channel copies at one site assumes the copies add in
  phase, which they do at one site in the simulator; with real separate dishes the
  correlator reports a cautious bound instead. The per-channel split of the shared
  capture noise attributes a co-channel copy heard in one channel partly to the other.
- The maps are single-epoch, one capture per pair. The live system adds rows every 5 s,
  so noise averages down while systematic errors stay. With TLE-class errors the
  calibrated ellipse would then shrink past the uncancelled residual (ephemeris-error
  description); the live server widens the position sigma of the satellites its clock
  checks put wrong for that (ephemeris allowance, LIMITATIONS.md section 7), which the
  maps, with no live calibrations, do not do.
- Detection thresholds assume a noise-like jammer on a quiet channel, with the tracker's
  start-up prior as its baseline (10 s of frames). A jammer under a busy authorized
  carrier is judged against that carrier's power instead.
- No terrain, multipath, interference from other users or real hardware timing was
  modeled. The node drivers for real radios are untested on hardware.

## 8. How the numbers were produced

From `locate/` with the project's virtual environment (Python 3.13, numpy, scipy,
sgp4):

```bash
OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 .venv/bin/python scripts/capability_study.py --workers 3
OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 .venv/bin/python scripts/capability_study.py --sections live
```

Options: `--sections detection,accuracy,references,limits,sites,montecarlo,scenarios,live`,
`--trials` (default 40), `--workers` (default 2), `--out` (default `docs/results`).

| file | what | how | time on this machine |
|---|---|---|---|
| `detection.json` | section 1 | `linkbudget.path_budget` for the nominal level; the server's `SignalTracker` on `SimWorld.render_psd` frames, 20 frames at 2 Hz from 4 start times 600 s apart, bisected to 0.25 dB; antenna gains and beamwidths from `core.antennas` | 2.5 min |
| `accuracy.json` | sections 2.1 to 2.5 | `core.accuracy.accuracy_map` at each point, epochs 03, 09, 15, 21 UTC on 2026-01-01; the coarse grid at 03 UTC; pairs from `sim.montecarlo.plan_event`; media delays per leg and per row from `core.iono` and `core.media`; the 03 UTC maps with other assumed media sizes; the reference moved 650 and 1,300 km at eight bearings | 3.4 min |
| `references.json` | section 6 | `path_budget` and `transponder_outputs` for every reference and channel at four epochs; `accuracy_map` with the reference at 5 or 8 kHz, at equal spectral density, in the earlier beam design, and at 20, 30 and 40 dBW over a map near Guam | 3 s |
| `montecarlo.json` | section 3 | `run_trials(draw_errors=True)`, 40 trials per run, t = 2026-01-01T03:00:00Z; seeds 1000 to 1013, one per (point, grade, calibration), shared by the media variants of that row (`media`: drawn, fixed-10, fixed-30, fixed-60, off, unmodelled-30) | 11.6 min (3 workers) |
| `limits.json` | sections 3 and 4.1 to 4.5 | noise-free truth solves; `plan_event` on solo channels; elevations over a sidereal day; `accuracy_map` for a 5 dBW CW; `run_trials` on uplink-cw and uplink-swept, 40 trials each, seed 31; the correlator's errors over its sigmas on two pairs, 100 noise realizations each | 3.4 min |
| `sites.json` | section 4.6 | the configuration with one site removed or one site kept; `plan_event` and `accuracy_map` at 03 UTC | 0.6 min |
| `scenarios.json` | section 5 | `run_trials(draw_errors=False)` on the shipped scenario files (and pacific-demo with 30 TECU and 2.3 m added), 4 trials per epoch, 8 epochs 00:00:12 to 21:00:12 UTC, seed 7 | 1.9 min (3 workers) |
| `live.json` | section 5 | `petal-locate demo --start 2026-01-01T03:00:00Z` on 127.0.0.1, presets loaded through `POST /api/v1/sim/presets/{name}/load`, read through `GET /api/v1/events/{id}` with the demo's API token until 16 rows are used | 2 min |

Every section except `live` is deterministic for a given code version and
configuration: a second full run reproduced all seven deterministic files exactly, apart
from timing. `config_sha256_16` in each file records the configuration used (the same in
all eight), and `media_assumed_by_solver` the media sizes the solver assumed. The live
run had the machine to itself (load average about 1). The scenario descriptions in
`config/scenarios/*.yaml` quote the server's chain as re-implemented in
`tests/test_sim_verify.py`, at four or eight epochs; that chain picks the primary by
detected SNR and captures references in a later slot, and its claims are asserted by
`test_sim_verify.py` and, live, by `tests/e2e/test_system.py`. The pacific-demo,
clock-fault and ephemeris-error descriptions were measured with the media terms in the
solver and agree with the table of section 5 (for example 0.45 to 0.61 km by 0.24 to
0.26 km calibrated in pacific-demo, against 0.47 to 0.61 km by 0.24 to 0.25 km here).
