# PETAL Locate — measurement model and estimation math (normative)

This document is the single source of truth for conventions. Code, tests and the
simulator must agree with it. Where the code and this document disagree, the bug is
in whichever one fails the physical sanity tests listed in section 12.

## 1. Units, frames, time

- SI everywhere inside the code: metres, seconds, hertz, watts, kelvin, radians.
  Config files and API payloads use degrees for angles and name the unit in the field
  (`lat_deg`, `bandwidth_hz`, `eirp_dbw`).
- Positions and velocities are **ECEF (WGS84)**. A velocity is the time derivative of
  the ECEF position, i.e. the velocity seen from the rotating Earth.
- Constants: `C = 299_792_458.0`, `OMEGA_E = 7.2921151467e-5` rad/s,
  `WGS84_A = 6378137.0`, `WGS84_F = 1/298.257223563`, `GM = 3.986004418e14`,
  Boltzmann `K_DBW = -228.6` dBW/K/Hz.
- **Absolute time** is `t_ns`: integer UTC nanoseconds since the Unix epoch (POSIX,
  no leap seconds). It must stay an integer end to end: a float64 at 1.7e9 s only
  resolves 240 ns. Float seconds (`t_s = t_ns * 1e-9`) are acceptable **only** for
  evaluating orbits and geometry (240 ns of time is under 1 mm of GEO motion in ECEF).
  All sample timing is `t0_ns` plus `n / fs` as float64 relative seconds.
- UT1 is taken equal to UTC plus a configurable `dut1_s` (default 0). Polar motion is
  ignored. Precise operator ephemerides should be supplied as ECEF tables, which
  bypass this.

## 2. Orbits

Every orbit type exposes `state_ecef(t_s) -> (r[...,3], v[...,3])`, vectorised over
`t_s`.

- `tle`: SGP4 (python `sgp4`), TEME to ECEF by a rotation of GMST about z
  (`gstime`), `v_ecef = R v_teme - omega x r_ecef`.
- `geo`: analytic circular geosynchronous orbit for slot-style definitions.
  Parameters `lon_deg`, `inclination_deg` (default 0), `epoch` (ISO time),
  `phase_deg` (default 0), `drift_deg_per_day` (default 0).
  Mean motion `n = OMEGA_E + drift`, radius `a = (GM / n^2)^(1/3)`.
  In the inertial frame that coincides with ECEF at `epoch`:
  `u(t) = phase + n (t - epoch)`,
  `r_i = Rz(lon - phase) Rx(inc) [a cos u, a sin u, 0]`, then
  `r_ecef = Rz(-OMEGA_E (t - epoch)) r_i`. With zero drift the ground track is a
  figure-8 centred on `lon_deg`; the satellite crosses the equator northbound when
  `u = 0 mod 2 pi`. Velocity analytically or by exact differentiation.
- `kepler`: classical elements at an epoch, two-body with secular J2 on RAAN, argument
  of perigee and mean anomaly; inertial frame is TEME-like (GMST rotation to ECEF).
- `table`: samples of `(t_ns, r_ecef, v_ecef)`, cubic Hermite interpolation; raises
  outside the table span.
- `PerturbedOrbit(base, ric_pos_m, ric_vel_mps)`: adds a constant offset expressed in
  the radial / in-track / cross-track frame built from the inertial velocity
  `v_ecef + omega x r`. Used for simulated ephemeris error and for consider-covariance
  partials.

## 3. Light-time delays (Sagnac included)

`Rz(a)` rotates a vector counter-clockwise by `a` about +z. All quantities are ECEF.

- Uplink, emitter at fixed `x_e`, signal arriving at the satellite at time `t`:
  `tau_u` solves `C tau_u = | r(t) - Rz(-OMEGA_E tau_u) x_e |`.
- Downlink, signal arriving at the fixed site `x_g` at time `t`:
  `tau_d` solves `C tau_d = | x_g - Rz(-OMEGA_E tau_d) r(t - tau_d) |`.
- Fixed-point iteration from the geometric range converges in 3 passes to < 1e-13 s.
- Bent pipe, received at the site at `t`, transponder group delay `tau_x`:
  `t_sat = t - tau_d(t) - tau_x`, `tau_u` evaluated at arrival time `t_sat`,
  `D(t) = tau_d(t) + tau_x + tau_u(t_sat)`.
- Direct terrestrial path (emitter heard by a site without a satellite):
  `D = |x_g - x_e| / C`, usable only inside the radio horizon
  `d < 4.12 km (sqrt(h_e) + sqrt(h_g))` (heights in metres above ground, 4/3 Earth).
- Ignoring the rotation terms costs up to about 0.2 microseconds of TDOA on GEO
  geometry, which is kilometres at small satellite separations. They are not optional.

Optional ionosphere (off by default, on in UHF error studies): group delay
`+40.3 TEC / f^2 / C` seconds and carrier phase advance of the same magnitude, with
slant factor `1 / sqrt(1 - (R_e cos(el) / (R_e + 350 km))^2)`, applied separately to
uplink (at `f_e`) and downlink (at `f_e + T`).

## 4. Signal model

An emitter radiates `s(t) exp(j 2 pi f_e t)`, complex envelope `s`, unit mean power.
A transponder of satellite `i` translates by `T_i` (signed, downlink = uplink + `T_i`,
no spectral inversion) with true value `T_i = T_nom,i (1 + eps_sat,i)` where
`eps_sat,i` is the satellite oscillator's fractional error.
A site channel tuned to `f_tune` with site frequency error `eps_g` and clock offset
`delta_g` (true time = labelled time + `delta_g`) records, for labelled time `t`:

```
t'      = t + delta_g
t_sat   = t' - tau_d(t') - tau_x
y(t)    = a * s(t_sat - tau_u(t_sat))
            * exp(j 2 pi [ (f_e + T_i - f_tune (1 + eps_g)) t'
                           - (f_e + T_i) (tau_d(t') + tau_x)
                           - f_e tau_u(t_sat) ])
          + other paths + noise
```

Baseband frequency of that path, with dots meaning d/dt at the site:

```
f_bb = (f_e + T_i - f_tune (1 + eps_g)) - (f_e + T_i) tau_d_dot - f_e tau_u_dot (1 - tau_d_dot)
```

(`tau_u_dot` here is the derivative of `tau_u` with respect to its own arrival time.)

Captures intended for correlation are tuned to the nominal downlink of the carrier,
`f_tune = f_e,nom + T_nom,i`, and both channels of a pair use the same sample rate
and the same nominal first-sample time.

A channel hears every path whose downlink (or direct) carrier falls in its passband:
all satellites, weighted by the antenna pattern at their off-boresight angle, plus
direct terrestrial paths. Co-channel satellites inside a wide beam therefore produce
several copies in one channel; section 7 says how the correlator copes.

### Amplitudes

Link budget in dB, per path (see `linkbudget.py`):

- `C/N0_up = EIRP_e(toward sat) - FSPL(f_e, rho_u) - L_pol + G/T_sat - K_DBW`
- Transponder: linear below saturation. A carrier with input flux
  `PFD = EIRP_e - 10 log10(4 pi rho_u^2)` leaves with
  `EIRP_c = EIRP_sat - (SFD - PFD)`. If the summed carrier EIRP would exceed
  `EIRP_sat`, every carrier and the relayed noise are scaled down together so the
  total equals `EIRP_sat` (power robbing). Relayed uplink noise leaves with density
  `EIRP_n0 = EIRP_c - C/N0_up` (same for every carrier, by construction).
- Site: `C_rx = EIRP_c - FSPL(f_d, rho_d) + G_site(off-axis)`, relayed noise density
  `N0_relay = EIRP_n0 - FSPL + G_site`, thermal `N0_site = K_DBW + 10 log10(T_sys)`.
- IQ scaling in the simulator: thermal noise has unit variance per complex sample at
  the capture rate (`N0_site * fs = 1`), so a path amplitude is
  `sqrt(C_rx / (N0_site fs))`.
- Relayed transponder noise is one deterministic waveform per transponder (seeded by
  satellite, transponder and absolute time) sent down every downlink path, so it is
  correlated between sites watching the same satellite and independent between
  satellites. Site thermal noise is independent per channel.

### Deterministic waveforms

`s(t)` is a pure function of absolute time and a seed, so separate node processes
synthesise identical emitters. `evaluate(base_ns: int, t_rel: float64[n])` returns
`s(base_ns * 1e-9 + t_rel)` without ever forming that sum in floating point:

- `cw`: constant 1.
- `chirp`: sawtooth linear FM, parameters sweep width and period; phase is analytic in
  `(time mod period)` with integer-nanosecond arithmetic for the modulo.
- `noise`: band-limited complex Gaussian. Defined on a base grid at
  `fs_base = 4 * bandwidth` (rounded to an integer number of nanoseconds per sample is
  NOT required; use the rational index `k = floor(t * fs_base)` computed with integer
  arithmetic). Grid samples come from a counter-based generator
  (`numpy.random.Philox(key=seed, counter=block_index)`), block size 4096, filtered to
  the bandwidth by a fixed FIR whose state is rebuilt from neighbouring blocks so
  block edges are seamless. Values between grid points use windowed-sinc
  interpolation (Kaiser, at least 16 taps); interpolation error below -60 dB.
- `psk`: BPSK/QPSK symbols from the same counter generator, root-raised-cosine shaped,
  same grid and interpolation machinery.
- Any waveform can be gated (`duty`, `period_s`) to model pulsed emitters.

## 5. Measurements

For channel A (satellite i via site a) and channel B (satellite j via site b), both
captured with labelled first sample `t0_ns` (differences in actual first-sample time
are corrected exactly), define

```
TDOA_AB = D_A - D_B            (seconds, evaluated at the capture midpoint)
FDOA_AB = f_bb,A - f_bb,B      (hertz,   evaluated at the capture midpoint)
```

including clock terms: a labelled-time offset `delta` makes the apparent delay
`D - delta`, so `TDOA_meas = (D_A - delta_a) - (D_B - delta_b)`.

The predictor used by the solver evaluates exactly the same expressions with nominal
oscillators and zero clock offsets:

```
h_tdoa(x) = D(x; i, a, t) - D(x; j, b, t)
h_fdoa(x) = f_bb(x; i, a, t) - f_bb(x; j, b, t)       with f_tune = f_e + T_nom
```

Downlink legs are part of `h`; nothing is "removed" from measurements by hand.

## 6. Cross-ambiguity function

```
chi(tau, nu) = sum_n  y_A(t_n + tau) * conj(y_B(t_n)) * exp(-j 2 pi nu t_n)
```

with `t_n` measured from the capture midpoint. `|chi|` peaks at
`tau = TDOA_AB`, `nu = FDOA_AB`.

Required behaviour of the estimator:

1. Coarse search over a delay window and a Doppler window supplied by the caller
   (from geometry: the extreme predicted values over the admissible emitter region,
   plus margin). Segment-and-FFT implementation: split into K segments short enough
   that `|nu_max| T_seg <= 0.25`, cross-correlate segments by FFT, FFT across segments
   for Doppler.
2. Fine search: sub-sample delay and sub-bin Doppler by local refinement (zero-padded
   / chirp-z interpolation or Newton on the exact CAF), bias under 5 % of the CRLB at
   30 dB post-correlation SNR.
3. Post-correlation SNR: peak power over the median of the surface away from all
   peaks, with the chi-square(2) median-to-mean correction. A detection needs
   `snr_db >= caf.min_snr_db` (default 13).
4. One-sigma estimates from Stein (section 8) using the measured SNR and the measured
   RMS bandwidth of the cross-spectrum. When the signal is too narrow for delay to be
   observable (`sigma_tdoa` above `caf.max_tdoa_sigma_s` or the delay cut is flat),
   return `tdoa = None` and keep the FDOA.
5. Exclusion zones: the caller passes predicted self-peaks (the same satellite seen in
   both channels, whose delay and Doppler are known exactly from the downlink
   geometry). They are notched before peak picking.
6. Mirror handling for co-channel cross-talk: the caller may pass a second admissible
   window for the swapped pairing (j via a against i via b). If a peak is found in each
   and they are within 6 dB, the result is flagged `mirror_ambiguous`.
7. Returns a small patch of the surface around the peak and the two 1-D cuts for
   display.

The CAF assumes the differential delay drifts by less than `0.5 / B` and the
differential Doppler by less than `1 / T` over the capture. The caller checks this
from predicted dynamics and shortens the capture or flags the result.

## 7. Detection and classification

- PSD frames: Welch, Hann, `nfft` bins, `navg` averages, in dB relative to an
  arbitrary but stable reference.
- Baseline per bin: slow quantile tracker (median of a rolling history) that freezes
  on bins currently in detection and adapts over `baseline_tau_s`.
- A signal is a run of contiguous bins (gaps up to `merge_bins`) above
  `baseline + threshold_db`, confirmed when seen in M of the last N frames, released
  after `release_frames` misses. Known carriers from the frequency plan are matched by
  frequency overlap and reported as `authorized` unless their power rises more than
  `power_anomaly_db` above their own baseline.
- Uplink frequency of a detection on satellite i: `f_up = f_down - T_nom,i`.
- Association: detections on channels pointed at the same satellite whose uplink
  ranges overlap are one in-transponder signal.
- Uplink versus local: `uplink` if seen through the same satellite at two or more
  sites farther apart than `min_site_separation_km`; `local` if seen at one site while
  at least one other healthy site watching the same satellite, with enough sensitivity
  to have seen it, does not; `undetermined` otherwise.
- Classification from a short IQ capture: occupied bandwidth, spectral flatness,
  kurtosis of the envelope, linearity of instantaneous frequency (sweep rate),
  cyclostationary symbol-rate line, on/off duty cycle. Classes: `cw`, `swept`,
  `noise`, `modulated`, `pulsed`, `unknown`, with a confidence.

## 8. Stein bounds

Noise bandwidth `B` (capture bandwidth), integration `T`, input SNRs `g1`, `g2` (in
`B`), effective `1/g = 0.5 (1/g1 + 1/g2 + 1/(g1 g2))`:

```
sigma_tdoa = 1 / (beta sqrt(B T g))        beta = 2 pi * B_rms
sigma_fdoa = 1 / (T_e  sqrt(B T g))        T_e  = 2 pi * T_rms = pi T / sqrt(3)   (rectangular window)
```

For a flat spectrum of width `B_s`, `B_rms = B_s / sqrt(12)`. Post-correlation SNR is
`2 B T g`.

## 9. Position solver

Unknown: emitter geodetic latitude and longitude at fixed height `h0` (default 0).
Measurements `z_k` (TDOA or FDOA, any mix of satellite pairs, sites and epochs), each
with `sigma_k`, optional calibration correction `c_k`:

```
minimise  sum_k ((z_k - c_k - h_k(lat, lon)) / sigma_k)^2
```

- Start: evaluate the cost on a coarse grid over the admissible region (points with
  elevation above `min_elevation_deg` to every satellite involved, intersected with
  the configured region of interest), refine the best cells with Levenberg-Marquardt
  (`scipy.optimize.least_squares`, numeric Jacobian with scaled steps), keep distinct
  minima whose cost is within `chi2_ambiguity` of the best.
- Equatorial geostationary satellites give a north/south mirror ambiguity for
  TDOA-only fixes. Mirrors are returned in `ambiguities`; the region of interest or
  FDOA resolves them, and the solution says which.
- Covariance in local east/north metres: `P_noise = (J^T W J)^-1` with `J` the
  Jacobian in metres. Error ellipse at confidence `p`: scale `sqrt(-2 ln(1 - p))`.
- Consider covariance for systematic errors. Consider parameters: per-satellite
  position (3, RIC) and velocity (3, RIC), per-site clock offset, per-satellite
  oscillator error, per-pair TDOA bias. With `H_c` the partials of `h` to those
  parameters and `S_c` their covariance:
  `K = (J^T W J)^-1 J^T W`, `P_total = P_noise + K H_c S_c H_c^T K^T`.
- Reference-emitter calibration (differential): a measurement of a known emitter at
  `x_ref` on the same pair, near in time, gives `c = z_ref - h(x_ref)`. Its variance
  adds to the measurement, and the consider partials become
  `H_c(x) - H_c(x_ref)` for satellite parameters and pair biases; clock terms cancel
  when target and reference come from the same captures.
- Goodness of fit: reduced chi-square with dof = measurements - 2.
- Stationary emitter assumed; multi-epoch fusion is just more rows.

## 10. Accuracy prediction

For a candidate emitter position and an assumed emitter (EIRP, antenna, pointing,
waveform bandwidth), compute path budgets, Stein sigmas, `J`, `P_total` and report
the 95 % semi-major axis, CEP50 and which term dominates. Evaluated over a grid this
is the planning map.

## 11. Lines of position

For display, the zero contour of `h(lat, lon) - z` over a grid (via `contourpy`) and
the `+/- k sigma` band, returned as GeoJSON.

## 12. Sanity tests every implementation must pass

1. Geostationary satellite, zero inclination: ECEF position constant to 1 mm, ECEF
   velocity zero; inclined 5 degrees: z amplitude `a sin(5 deg)`, peak ECEF speed
   about `a * 0.0873 * OMEGA_E` (268 m/s).
2. Emitter exactly under a GEO satellite: `tau_u = 35786 km / C` within 1 m.
3. Sagnac: for an emitter on the equator 60 degrees east of a GEO satellite, the
   light-time delay differs from the plain ECEF range by the analytic
   `OMEGA_E (x_e x r)_z / C^2` within 1 ps.
4. Simulator round trip: an IQ capture of a noise emitter through two satellites,
   estimated by the CAF, returns TDOA and FDOA equal to section 5 predictions within
   3 sigma of the Stein bound; swapping A and B flips both signs.
5. A positive clock offset `delta_a` lowers `TDOA_AB` by `delta_a`.
6. Raising the satellite oscillator error of satellite i by `eps` raises `FDOA_AB` by
   `eps * T_nom,i`.
7. Solver: noise-free measurements return the true position within 1 m; with Gaussian
   noise, the normalised estimation error squared averages 2 (within Monte Carlo
   tolerance) and about 95 % of fixes fall inside the 95 % ellipse.
8. Mirror: three equatorial GEO satellites, TDOA only, emitter at 30 N: both
   +30 and -30 latitude are returned.
