# PETAL Locate

PETAL Locate watches bent-pipe satellite transponders for interference with a network of
small ground sites, decides whether an interfering signal enters on the satellite uplink
(and so reaches everyone using that transponder) or only at one site's own receiver, and
locates uplink emitters on the ground from the time and frequency differences (TDOA and
FDOA) between the copies two or more satellites relay, calibrated against reference
transmitters at known places. Each site is an antenna (a PETAL 3D-printed dish or any
other), a software-defined radio and a node agent that streams spectrum to a central
server; the server raises events, asks the sites for coincident IQ captures, correlates
them, solves for position and serves an operator console. Satellites, transponders,
frequency plans, sites and antennas are all configuration.

**This is a proof of concept validated only against its own simulator.** Every number
and picture here is simulated with illustrative link parameters and nominal orbital
slots; nothing has been tested against real satellites or radios
([LIMITATIONS.md](docs/LIMITATIONS.md)).

![Event dossier in the console: a simulated uplink jammer located 0.02 km from the truth inside a 0.31 x 0.04 km ellipse, its lines of position and calibrated measurements](docs/images/event-location.png)

## Quick start

From this directory (Linux, Python 3.11 or later):

```bash
python3 -m venv .venv
.venv/bin/pip install .
.venv/bin/petal-locate demo --port 0
```

The demo starts the server with the example configuration (`config/pacific-uhf.yaml`:
three UHF satellites, seven sites from Honolulu to Manila, two reference carriers) and
one simulated node process per site over real WebSockets, then prints the console
address:

```
PETAL Locate demonstration (SIMULATED data)
  Console:   http://127.0.0.1:44309/
  ...
All 7 nodes are online.
```

Open that address in a browser. A jammer in the Philippine Sea is detected within
seconds, judged an uplink signal and located: in the run above it was located 7.3 s after
it was first detected, 104 m from the simulated truth inside a 423 m (95 %) ellipse.
Other scenarios are loaded from the **Simulation** screen. Ctrl-C stops the server and
every node. To deploy on real sites, see [OPERATIONS.md](docs/OPERATIONS.md).

## What each demo scenario shows

All in `config/scenarios/`; each file's `description` gives the measured numbers.

| scenario | the simulated world | what the operator should see |
|---|---|---|
| `pacific-demo` (default) | a 10 kHz noise jammer, 20 dBW, at 20 N 132 E on a channel all three satellites relay; small satellite, oscillator and clock errors | a new signal on every site, an uplink verdict within seconds, a fix 70 to 190 m from the truth inside a 0.4 to 0.6 km by 0.1 km ellipse (calibrated against the Guam reference) |
| `clock-fault` | the same, with the Naha site clock 10 us late | the same calibrated fix; without calibration the fix would be 14 to 34 km off with no warning from the fit |
| `ephemeris-error` | the jammer at 17 N 141 E while two satellites are 8 km and 6 km from where the server believes them | calibrated 30 to 300 m from the truth; uncalibrated 12 to 37 km off |
| `two-jammers` | a second jammer, 8 kHz, 18 dBW, at 30 N 135 E on the other shared channel | two separate events, each located (40 to 500 m and 60 to 160 m) |
| `uplink-cw` | an unmodulated carrier, 5 dBW, at 15 N 130 E | classification `cw`, uplink, FDOA only ("No TDOA: the signal is too narrow ..."), a coarse fix 10 to 65 km off inside a very long ellipse |
| `uplink-swept` | a sawtooth chirp sweeping 10 kHz every 40 ms at 21 N 127 E | classification `swept`, uplink, every correlation ambiguous, no position, and a message saying why |
| `local-interferer` | a weak terrestrial transmitter 4 km from Busan on a downlink frequency | seen only at Busan; verdict local ("Local interference at Busan"), advice to relocate, shield or filter there, no location attempt |
| `quiet` | only the two reference carriers | the references shown as authorised carriers, no events |

## What it can do (simulated)

From [RESULTS.md](docs/RESULTS.md), on the example configuration; every number is
simulated, with illustrative link parameters and nominal slots:

- **Detection.** Every site detects an uplink jammer on any satellite it can see. With
  the 0.72 m PETAL dish a 10 kHz jammer needs about 11 dBW EIRP toward the satellite
  (15 dBW if it fills a 25 kHz channel); a 1.2 m dish or a 10 dBi beam antenna detects
  jammers 3 to 3.5 dB weaker. At these frequencies (wavelength about 1.15 m) the
  0.72 m dish is a 3.3 dBi broad-beam reflector, not a focusing dish.
- **Location, operator-grade ephemeris.** Uncalibrated 95 % error ellipses of about 4 to
  6 km by 0.6 km; Monte Carlo median miss 1.3 to 2.1 km. Calibrated against a reference
  transmitter: about ten times smaller, median miss 140 to 280 m.
- **Location, TLE-grade ephemeris.** Uncalibrated about 23 km median miss; calibrated
  median miss 0.4 km near the reference and 0.9 km 1,500 km away, and a predicted 95 %
  semi-major of 4 to 24 km at about 3,000 km.
- **Cannot:** locate a signal only one satellite relays; locate a CW jammer without
  calibration (calibrated, about 19 km median); locate a periodically swept jammer at
  all; always pick between two candidate positions without calibration.
- **Sites** buy redundancy and detection margin, not location accuracy: any one site
  with channels on all three satellites predicts within about 25 % of the seven-site
  network.

## Documents

| document | what it holds |
|---|---|
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | deploying and running it: site hardware, install, configuration, server, nodes, console, calibration, troubleshooting |
| [docs/LIMITATIONS.md](docs/LIMITATIONS.md) | everything this proof of concept does not prove or do |
| [docs/RESULTS.md](docs/RESULTS.md) | the simulated capability study; raw numbers in `docs/results/*.json`, reproduced by `scripts/capability_study.py` |
| [docs/MATH.md](docs/MATH.md) | the measurement model and estimation (normative: signs, units, algorithms, tolerances) |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | modules, configuration model, simulator, server pipeline, CLI |
| [docs/PROTOCOL.md](docs/PROTOCOL.md) | the node-to-server and console-to-server wire protocols |
| [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) | rules for changing the code |
| [docs/UI-BRIEF.md](docs/UI-BRIEF.md), [ui/README.md](ui/README.md), [ui/DESIGN.md](ui/DESIGN.md) | the operator console: brief, build, design record |

More console screenshots: [watch](docs/images/watch.png),
[measurements and correlation](docs/images/event-measurements.png),
[spectrum](docs/images/spectrum.png), [accuracy planning](docs/images/plan-accuracy.png),
[configuration](docs/images/configure.png).

## Running the tests

With the development install of [CONTRIBUTING.md](docs/CONTRIBUTING.md):

```bash
OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 .venv/bin/python -m pytest -q -p no:cacheprovider -m "not slow" tests/test_config.py tests/test_protocol.py
```

That subset ran 364 tests in 1.7 s. Leave out the file names for the whole suite;
leave out `-m "not slow"` to include the long statistical tests and the end-to-end
tests, which start the real demo (`tests/e2e/test_system.py`, about 4 minutes) and drive
the console in Chromium with Playwright (`tests/e2e/test_console.py`; skipped when Node,
the console's dependencies or the browser are missing). The console's own tests are in
`ui/` (`npm test`, `npm run e2e`; see [ui/README.md](ui/README.md)).
