# PETAL Locate — measurement model and estimation math (normative)

This document is the single source of truth for conventions. Code, tests and the
simulator must agree with it. Where code and this document disagree, the bug is in
whichever one fails the physical sanity tests in section 12.

Revision 3: describes the implementation as built and verified. Where the code departs
from revision 2 the text states the rule the code follows, with the reason when it was
a judgement call, and section 12 states tolerances that hold. Numbers quoted as
"measured" come from the review of revision 1 or from the build and verification
scripts of revision 3.

## 1. Units, frames, time

- SI everywhere inside the code: metres, seconds, hertz, watts, kelvin, radians.
  Config files and API payloads use degrees for angles and name the unit in the field
  (`lat_deg`, `bandwidth_hz`, `eirp_dbw`).
- Positions and velocities are **ECEF (WGS84)**. A velocity is the time derivative of
  the ECEF position. (For `tle` orbits the velocity returned by SGP4 differs from
  dr/dt by up to 0.06 m/s; it is used only to build RIC frames and for display.
  Delay rates never come from state velocity, see section 3.)
- Constants: `C = 299_792_458.0`, `OMEGA_E = 7.2921151467e-5` rad/s,
  `WGS84_A = 6378137.0`, `WGS84_F = 1/298.257223563`, `GM = 3.986004418e14`,
  Boltzmann `K_DBW = -228.6` dBW/K/Hz, mean Earth radius for the ionosphere and radio
  horizon `R_E = 6371e3`.
- **Absolute time** is `t_ns`: integer UTC nanoseconds since the Unix epoch (POSIX,
  no leap seconds). It stays a Python `int` end to end: a float64 at 1.7e9 s only
  resolves 240 ns. Float seconds (`t_s = sec(t_ns)`, the correctly rounded quotient
  `t_ns / 10**9`, which can differ from `t_ns * 1e-9` by one ulp) are acceptable
  **only** for evaluating orbits and geometry. A float time beyond
  `MAX_UNIX_S = 1e11` s (year 5138) is an integer `t_ns` or milliseconds passed as
  seconds: orbits, geometry and `gmst_rad` reject it with `ValueError`. All sample
  timing is `t0_ns` plus `n / fs` as float64 relative seconds. **Never multiply a
  frequency by absolute seconds.**
- Earth rotation angle for inertial orbits (`tle`, `kepler`) is linear in time:
  `theta = gmst_rad(t_s, dut1_s) = (GMST_REF + OMEGA_GMST * ((t_s - T_REF_S) + dut1_s)) mod 2 pi`
  with `T_REF_S = 1767225600.0` (2026-01-01T00:00:00Z),
  `GMST_REF = 1.756863409365046` rad (`sgp4.propagation.gstime(2461041.5)`),
  `OMEGA_GMST = 7.2921158554e-5` rad/s. It agrees with `gstime` to under 5e-8 rad
  (2 m at GEO) from 2024 to 2031 and is smooth: the product
  `OMEGA_GMST (t_s - T_REF_S)` is formed exactly (double-double,
  `timebase.wrapped_phase`) before the reduction, where the rounded product would jitter
  by 1e-12 rad from sample to sample (4e-5 m at GEO); the `geo` rotation angle and the
  `kepler` mean anomaly use the same helper. Never call `gstime` on a per-sample
  float Julian date (its 40 microsecond quantisation puts 6 cm of jitter on the
  position). UT1 = UTC + `dut1_s` (default 0); polar motion is ignored. Precise
  operator ephemerides should be supplied as ECEF tables, which bypass all of this.
- Site and emitter positions are antenna phase centres:
  `lla_to_ecef(lat_deg, lon_deg, alt_m + antenna_height_m)`. Every module gets site
  positions from `SystemConfig.site_xyz(site_id)`, never from `alt_m` alone.

## 2. Orbits

Every orbit type exposes `state_ecef(t_s) -> (r[...,3], v[...,3])`, vectorised over
`t_s`. `Rz(a)` rotates a vector counter-clockwise by `a` about +z (active rotation).
The orbit classes also accept `state_ecef(t_s, dt_s)` and `position_ecef(t_s, dt_s)`
(and declare `supports_split_time`): the state at `t_s + dt_s` without forming that sum
in float64, which resolves 2.4e-7 s (2 mm of low-orbit motion). The light-time code
needs this for `t - tau` (section 3); `orbits.orbit_position` / `orbit_state` evaluate
any orbit that way, and an object implementing only the protocol at the rounded sum.

- `tle`: SGP4 (python `sgp4`) called with split time
  (`jd = 2440587.5 + floor(t_s / 86400)`, `fr = (t_s mod 86400) / 86400`).
  `r_ecef = Rz(-theta) r_teme`,
  `v_ecef = Rz(-theta) v_teme - [0, 0, OMEGA_GMST] x r_ecef`.
- `geo`: analytic circular geosynchronous orbit for slot-style definitions.
  Parameters `lon_deg`, `inclination_deg` (default 0), `epoch` (ISO time),
  `phase_deg` (default 0), `drift_deg_per_day` (default 0).
  `n = OMEGA_E + drift`, `a = (GM / n^2)^(1/3)`
  (`a = 42 164 172.37 m` for zero drift). In the inertial frame that coincides with
  ECEF at `epoch`: `u(t) = phase + n (t - epoch)`,
  `r_i = Rz(lon - phase) Rx(inc) [a cos u, a sin u, 0]`,
  `r_ecef = Rz(-OMEGA_E (t - epoch)) r_i`. With zero drift the ground track is a
  figure-8 centred on `lon_deg`, crossing the equator northbound when `u = 0 mod 2 pi`.
  Velocity is the exact derivative.
- `kepler`: classical elements at an epoch, two-body with secular J2 on RAAN, argument
  of perigee and mean anomaly; inertial frame rotated to ECEF with `theta` above. With
  `p = a (1 - e^2)`, `n0 = sqrt(GM / a^3)` and `k = 1.5 J2 (WGS84_A / p)^2`: mean motion
  `n = n0 (1 + k sqrt(1 - e^2) (1 - 1.5 sin^2 i))`, `raan' = -k n cos i`,
  `argp' = k n (2 - 2.5 sin^2 i)`. Velocity is the exact derivative, node, perigee and
  Earth-rotation rates included.
- `table`: samples of `(t, r_ecef, v_ecef)`, cubic Hermite interpolation; raises
  outside the table span.
- `PerturbedOrbit(base, ric_pos_m, ric_vel_mps, t_ref_s)`:
  `r'(t) = r(t) + Q(t) [ric_pos_m + ric_vel_mps (t - t_ref_s)]`, where the columns of
  `Q(t)` are the radial, in-track and cross-track unit vectors of the base orbit at
  `t` (cross-track along `r x (v_ecef + omega x r)`, in-track = cross x radial).
  The returned velocity is `d r'/dt`, a fourth-order central difference of `r'` with a
  0.125 s step (a power of two, exact at Unix magnitudes where 0.1 s is not; a
  second-order difference would leave 2e-5 m/s in low orbit), so `state_ecef` needs the
  base orbit 0.25 s either side; `ric_vel_mps` is never simply added to `v`.
  `t_ref_s` is the measurement epoch when forming consider-covariance partials
  (section 9) and the scenario start for simulated truth errors.

## 3. Light-time delays (Sagnac included)

All quantities ECEF.

- Uplink, emitter at fixed `x_e`, signal arriving at the satellite at time `t`:
  `tau_u` solves `C tau_u = | r(t) - Rz(-OMEGA_E tau_u) x_e |`.
- Downlink, signal arriving at the fixed site `x_g` at time `t`:
  `tau_d` solves `C tau_d = | x_g - Rz(-OMEGA_E tau_d) r(t - tau_d) |`.
- Downlink: fixed-point iteration from the geometric range, 3 passes. Uplink: one pass
  started from the geometric range plus the first-order rotation term,
  `|r - x_e| / C + OMEGA_E (x_e x r)_z / C^2`; the start is within a picosecond and the
  contraction is 1.5e-6, so one pass ends where three from the bare range do (within
  3e-16 s of an inertial event simulation). The satellite is evaluated at `t - tau`
  with the two terms kept apart (section 2); an orbit implementing only the protocol
  gets the rounded difference (about 1e-15 s of delay scatter on GEO, up to 2e-12 s in
  low orbit).
- Bent pipe, received at the site at `t`, transponder group delay `tau_x`:
  `t_sat = t - tau_d(t) - tau_x`, `tau_u` evaluated at arrival time `t_sat`,
  `D(t) = tau_d(t) + tau_x + tau_u(t_sat)`.
- **Rates.** `tau_d_dot = d tau_d / dt` (t = receive time),
  `tau_u_dot = d tau_u / d t_sat` (its own arrival time),
  `D_dot = tau_d_dot + tau_u_dot (1 - tau_d_dot)`. They are derivatives of the delay
  functions above, computed by differencing those functions:
  `f'(t) = [8 (f(t+h) - f(t-h)) - (f(t+2h) - f(t-2h))] / (12 h)` with `h = 0.5 s`
  (a power of two, so `t +/- h` is exact in float64), applied to `D` and `tau_d` of
  `bent_pipe` in reception time; `tau_u_dot` then follows from the identity above, not
  from a separate stencil about `t_sat` (the two agree to 2e-15 s/s), so one routine
  gives all three. Measured error: below 1e-15 s/s on GEO geometry, about 6e-15 s/s
  (2e-6 Hz at 300 MHz) on a 600 km overhead pass. They are never computed from the
  velocity returned by `state_ecef`. Every delay rate used anywhere (predictors,
  simulator truth, consider partials) comes from this one routine, so the predicted
  FDOA is the FDOA present in simulated IQ.
- **Visibility.** A satellite leg exists only when the satellite is above the horizon
  of its ground end: elevation > 0 at the emitter for the uplink and at the site for
  the downlink (WGS84 local vertical, no refraction). A path with either leg below the
  horizon is not rendered and has no budget; a satellite below a site's horizon never
  appears in that site's channels, whatever the antenna pattern. The admissibility
  tests of the solver, the lines of position and the search windows count an
  elevation equal to the configured mask (default 0) as visible; the boundary case
  only widens a window marginally.
- Direct terrestrial path (emitter heard by a site without a satellite):
  `D = |x_g - x_e| / C`, only inside the radio horizon
  `d < 4.12 km (sqrt(h_e) + sqrt(h_g))` with antenna heights in metres.
- Ignoring the rotation terms costs up to 0.19 microseconds per leg (0.35 differential)
  on GEO geometry, which is kilometres at small satellite separations.

Optional ionosphere (off by default, on in UHF error studies): group delay
`delta = 40.3 * STEC / (f^2 C)` seconds with `f` in Hz and `STEC` in electrons/m^2:
`STEC = vtec_tecu * 1e16 * slant(el)`,
`slant(el) = 1 / sqrt(1 - (R_E cos(el) / (R_E + 350 km))^2)`, `el` = elevation of the
satellite seen from the ground end of the leg (emitter for the uplink at `f_e`, site
for the downlink at `f_e + T`). The envelope argument uses `delay + delta`, the
carrier-phase terms use `delay - delta`. Check: 30 TECU, 305 MHz, zenith = 434 ns.

## 4. Signal model

An emitter radiates `s(t) exp(j 2 pi f_e t)`, complex envelope `s`, unit mean power.
A transponder of satellite `i` translates by `T_i` (signed, downlink = uplink + `T_i`,
no spectral inversion) with true value `T_i = T_nom,i (1 + eps_sat,i)`; `eps_sat,i`
is the satellite oscillator's fractional error, common to all its transponders.
A site channel tuned to `f_tune` with site oscillator error `eps_g` and clock offset
`delta_g` (**true time = labelled time + `delta_g`**) records, for labelled time `t`:

```
t'      = t + delta_g
t_sat   = t' - tau_d(t') - tau_x
y(t)    = a * s(t_sat - tau_u(t_sat))
            * exp(j 2 pi [ (f_e + T_i - f_tune (1 + eps_g)) t'
                           - (f_e + T_i) (tau_d(t') + tau_x)
                           - f_e tau_u(t_sat) ])
          + other paths + noise
```

Implementation: the first phase term is evaluated as
`(f_e + T_i - f_tune (1 + eps_g)) * (n / fs + delta_g)` with `n / fs` the float64 time
since the labelled first sample. The omitted product with the first-sample time is a
per-path, per-capture constant phase on which no measurement depends.

Baseband frequency of that path (dots as defined in section 3):

```
f_bb = (f_e + T_i - f_tune (1 + eps_g)) - (f_e + T_i) tau_d_dot - f_e tau_u_dot (1 - tau_d_dot)
```

Captures intended for correlation are tuned to the nominal downlink of the carrier,
`f_tune = f_e,nom + T_nom,i`, and both channels of a pair use the same sample rate
and the same requested first-sample time.

**Which paths a channel hears.** An emitter is relayed by a transponder if and only
if its centre frequency lies in the transponder's uplink passband. A channel hears
every visible satellite's downlink (weighted by its antenna pattern at that
satellite's off-boresight angle) and every direct terrestrial path, when the path's
occupied band overlaps the capture band. Co-channel satellites inside a wide beam
therefore produce several copies in one channel; section 6 says how the correlator
copes.

**Receiver channel filter.** A capture at `(fc, fs)` contains only what lies in
`[fc - fs/2, fc + fs/2]`; nothing may alias in. For a path with baseband offset
`off = f_e + T_i - fc` and waveform bandwidth `W` (`Waveform.bandwidth_hz`, 0 for
`cw`):

- wholly inside (`|off| + W/2 <= fs/2`): rendered directly at `fs`;
- wholly outside (`|off| - W/2 >= fs/2`): omitted;
- straddling: `cw` and `chirp` are synthesised at `m fs` (smallest integer `m` that
  contains the band) and decimated by `m` through a linear-phase FIR (52 m + 1 taps,
  passband to 0.4 fs, at least 80 dB beyond 0.5 fs) whose group delay is removed
  exactly; `decimate` takes its input as zero outside the array, so
  `decimate_margin(m)` (26) extra output samples are rendered at each end and dropped.
  `noise` and `psk` are treated the same way up to `m = 4`. Wider ones are replaced,
  for that capture, by their **in-band slice**: a `noise` waveform of bandwidth `fs`
  centred on `fc`, power `C_rx fs / W`, rendered at `2 fs` and decimated, keyed by
  `(source key, round(fc - T_i) in Hz, round(fs))` so both channels of a pair (same
  uplink centre and rate) see the same slice.

### Amplitudes

Link budget in dB, per path (`linkbudget.py`):

- `C/N0_up = EIRP_e(toward sat) - FSPL(f_e, rho_u) + G/T_sat - K_DBW`
- Transponder, linear below saturation. A carrier with input flux
  `PFD = EIRP_e(toward sat) - 10 log10(4 pi rho_u^2)` leaves with
  `EIRP_c = EIRP_sat - (SFD - PFD)`.
- Transponder noise is a property of the transponder alone:
  `EIRP_n0 = EIRP_sat - SFD + 10 log10(4 pi f_uc^2 / C^2) - G/T_sat + K_DBW` dBW/Hz,
  `f_uc = uplink_center_hz` (this equals `EIRP_c - C/N0_up` for any carrier and is
  defined with no carrier present).
- Power robbing: `P_tot = sum of carrier EIRPs + EIRP_n0 * bandwidth_hz` in watts. If
  `P_tot > EIRP_sat`, every carrier and `EIRP_n0` are multiplied by
  `EIRP_sat / P_tot`.
- Site: `C_rx = EIRP_c - FSPL(f_d, rho_d) + G_site(off-axis) - L_pol`,
  `N0_relay = EIRP_n0 - FSPL(f_d, rho_d) + G_site(off-axis) - L_pol`,
  `N0_site = K_DBW + 10 log10(T_sys)`, with `L_pol` the receive channel's
  `polarization_loss_db`.
- Direct path: `C_rx = EIRP_e(toward site) - FSPL(f_e, d) + G_site(off-axis angle to
  the emitter) - L_pol`.
- `EIRP_e(toward x) = eirp_dbw + G_e(off-axis toward x) - G_e(boresight)`: the
  configured EIRP is on boresight, and `"omni"` applies no pattern.
- IQ scaling in the simulator: thermal noise has unit variance per complex sample at
  the capture rate (`N0_site * fs = 1`), so a carrier path has amplitude
  `sqrt(C_rx / (N0_site fs))`.
- Relayed transponder noise of transponder `x` on satellite `i` is a unit-power
  `noise` waveform `n_x` of bandwidth `B_x` centred on the transponder downlink centre
  `f_xd`, a function of satellite time, sent down each downlink path like an emitter:
  `y_n(t) = a_n n_x(t' - tau_d(t')) exp(j 2 pi [(f_xd - f_tune (1 + eps_g)) (n/fs + delta_g) - f_xd tau_d(t')])`,
  `a_n^2 = N0_relay B_x / (N0_site fs)`, under the same channel-filter rule (so wide
  transponders use the in-band slice, power `N0_relay / N0_site` per sample). It is
  therefore correlated between sites watching the same satellite and independent
  between satellites. Site thermal noise is independent per channel.

### Deterministic waveforms

`s(t)` is a pure function of absolute time and a key, so separate node processes
synthesise identical emitters. `evaluate(base_ns: int, t_rel: float64[n])` returns
`s(base_ns * 1e-9 + t_rel)` without ever forming that sum in floating point.

**Keys.** Every generator key is a 128-bit integer from a process-independent hash,
never Python's `hash()`:
`key128 = int.from_bytes(hashlib.blake2b(label.encode(), digest_size=16).digest(), "little")`
with label `f"emitter|{scenario.seed}|{emitter.id}|{waveform.seed}"` for emitter
waveforms, `f"relay|{scenario.seed}|{sat_id}|{transponder_id}"` for relayed noise
(append `|{round(fc_up)}|{round(fs)}` for an in-band slice), and
`f"thermal|{scenario.seed}|{channel_id}|{t_start_ns}|{fs}|{fc}"` for site thermal
noise. All nodes of a deployment must run the same NumPy release.

**Grid arithmetic.** The grid rate is an integer number of hertz:
`fs_base_hz = ceil(4 * bandwidth_hz)` as a Python int for `noise`, `4 * symbol_rate_hz`
for `psk` (four samples per symbol, so every symbol sits on a grid point). In `evaluate`:
`q, r = divmod(base_ns * fs_base_hz, 10**9)` with Python ints (never numpy int64,
never a float rate); then in float64 `x = r * 1e-9 + t_rel * fs_base_hz`,
`k = q + floor(x)`, `mu = x - floor(x)`. `symbol_rate_hz` is an integer number of
hertz and uses the same divmod. Chirp and gate periods are integer nanoseconds:
`period_ns = round(period_s * 1e9)`,
`u = ((base_ns % period_ns) * 1e-9 + t_rel) mod (period_ns * 1e-9)`.

- `cw`: constant 1; `Waveform.bandwidth_hz` is 0 whatever the spec says.
- `chirp`: sawtooth linear FM, sweep width `bandwidth_hz` and period
  `sweep_period_s`; phase `pi bandwidth_hz u (u - P) / P` radians (`P` the period),
  continuous through the fly-back.
- `noise`: band-limited complex Gaussian on the base grid. Raw complex grid sample
  `k` (block `b = k // 4096`, `m = k % 4096`, floor division) is
  `(x[2m] + 1j x[2m+1]) / sqrt(2)` with
  `x = numpy.random.Generator(numpy.random.Philox(key=key128, counter=numpy.array([0, b, 0, 0], dtype=numpy.uint64))).standard_normal(8192)`.
  (The low counter word is the one the generator increments; putting the block index
  there makes consecutive blocks shifted copies of each other.) The band-limited grid
  sample is the plain convolution `g[k] = sum_j h[j] raw[k - j]`, FIR length at most
  4097 so only adjacent blocks are needed, normalised to unit power. Values between
  grid points use windowed-sinc interpolation (Kaiser, at least 16 taps);
  interpolation error below -60 dB. The band-limiting FIR has 4097 taps (Kaiser, 90 dB)
  with its stopband edge at `bandwidth_hz / 2`, so nothing above -90 dB lies outside
  the nominal band; the flat part is about 0.99 `bandwidth_hz` and `rms_bandwidth_hz`
  is computed from the actual filter (0.9934 `bandwidth_hz / sqrt(12)`). The
  interpolator is a 16-tap Kaiser (beta 12) windowed sinc read from a 2048-phase table
  with linear interpolation between phases (table error -140 dB). A negative block
  index (before 1970) uses the counter word `b mod 2**64`.
- `psk`: BPSK/QPSK symbols from the same block generator (BPSK `sign x[2m]`, QPSK
  `(sign x[2m] + 1j sign x[2m+1]) / sqrt(2)`), zero-stuffed on the grid and shaped by a
  root-raised-cosine FIR sampled at 4 per symbol (4097 taps, +/- 512 symbols, Kaiser
  beta 8 taper, unit mean power); same
  interpolation machinery. The causal FIR delays generator element `c[n]` by 512
  symbols: the symbol centred on `n / symbol_rate_hz` is `c[n - 512]`
  (`PskWaveform.symbols`). From a spec, `symbol_rate_hz` is used when non-zero (a
  non-zero `bandwidth_hz` must then be at least `symbol_rate_hz (1 + rolloff)`),
  otherwise `floor(bandwidth_hz / (1 + rolloff))`, so the occupied band never exceeds
  the declared one; `Waveform.bandwidth_hz` is `symbol_rate_hz (1 + rolloff)`.
- Any waveform can be gated (`duty`, `period_s`) to model pulsed emitters: on for the
  first `duty * period` of every period (phase counted from the Unix epoch, hard
  edges), unit mean power while on, so mean power `duty`. `spectral_shape` and the
  bandwidths describe the ungated envelope; `Waveform.gate_fraction(base_ns,
  duration_s)` is the exact on-fraction of a snippet.
- Site thermal noise (`channel.thermal_noise`): `Philox(key=key128)` with the default
  zero counter, `x = standard_normal(2 n)`, sample `k` = `(x[2k] + 1j x[2k+1]) / sqrt(2)`.

## 5. Measurements

For channel A (satellite i via site a) and channel B (satellite j via site b) captured
at the same rate, with labelled first-sample times `tA_ns`, `tB_ns`:

```
t_mid_ns  = tB_ns + round(1e9 (N - 1) / (2 fs))          midpoint of the channel-B samples; Measurement.t_ns
TDOA_AB   = D_A(t_mid + TDOA_AB) - D_B(t_mid)            seconds
FDOA_AB   = f_bb,A(t_mid + TDOA_AB) - f_bb,B(t_mid)      hertz
```

including clock terms: a labelled-time offset `delta` makes the apparent delay
`D - delta`, so `TDOA_meas = (D_A - delta_a) - (D_B - delta_b)`.

The predictor used by the solver evaluates the same expressions with nominal
oscillators and zero clock offsets:

```
h_tdoa(x) = D(x; i, a, t_mid + h_tdoa) - D(x; j, b, t_mid)     one fixed-point pass from D_A(t_mid) - D_B(t_mid)
h_fdoa(x) = f_bb(x; i, a, t_mid + h_tdoa) - f_bb(x; j, b, t_mid)      with f_tune = f_e + T_nom
```

Path A at `t_mid + h_tdoa` is read from the quartic through the five stencil samples of
section 3, not from new orbit evaluations; the predictors raise `ValueError` when
`|D_A - D_B| > 1 s`. With `f_tune = f_e + T_nom` each path's `f_bb` reduces to
`-f_e D_dot - T_nom tau_d_dot`. `geometry.predict_pair` returns both rows from one
evaluation. Because A is evaluated at the shifted epoch, swapping A and B negates the
prediction only to within `h_AB + h_BA ~ h_AB (D_dot_A - D_dot_B)`, and the single pass
leaves `D_dot_A^2 |TDOA|` against the converged definition: about 1e-16 s on GEO pairs,
up to 8.5e-11 s with a low-orbit satellite on path A.

Downlink legs are part of `h`; nothing is "removed" from measurements by hand.

If a radio reports centre frequencies `fc_A`, `fc_B` that differ from the request,
`z_fdoa = nu_peak + (fc_A - T_nom,i) - (fc_B - T_nom,j)` and
`PathSpec.f_e_hz = fc_A - T_nom,i`.

## 6. Cross-ambiguity function

```
chi(tau, nu) = sum_n  y_A(t_n + tau) * conj(y_B(t_n)) * exp(-j 2 pi nu t_n)
```

with `t_n` measured from the midpoint of the channel-B samples. `|chi|` peaks at
`tau = TDOA_AB`, `nu = FDOA_AB` as defined in section 5.

Array convention: `ya[m]` is `y_A` at labelled time `tA + m/fs`, `yb[n]` is `y_B` at
`tB + n/fs`; `t0_offset_s = (tA_ns - tB_ns) * 1e-9` from the integer difference. A
peak at array lag `l` (ya advanced by `l` relative to yb) is reported as
`tdoa_s = l + t0_offset_s`. `delay_window`, `exclude`, `mirror_window`,
`delay_axis_s` and the returned `tdoa_s` are all true A-minus-B values; the estimator
converts to array lag internally. No Doppler correction is applied for the offset.

Required behaviour of the estimator:

0. Band limiting: both channels pass the same linear-phase FIR (Kaiser, 60 dB stop
   band when the capture is long enough, at most `min(n_A, n_B) / 8` taps, transition
   5 % of the pass band), centred on the capture centre, whose pass band is the
   caller's occupied band `signal_bw_hz` plus twice the largest Doppler magnitude of
   the primary and mirror windows, so a Doppler-shifted copy is not clipped (it matters
   for signals a few hundred hertz wide). The filter transient is trimmed from both
   channels. `None`, or a pass band leaving under 1 % of `fs` to remove, means no
   filter. `B` in section 8 is the noise bandwidth of this filter (`fs` without one),
   reported as `CafResult.noise_bandwidth_hz`, and `g1`, `g2` are SNRs in it.
1. Coarse search over a delay window and a Doppler window supplied by the caller.
   Remove the Doppler-window centre `nu_c` by multiplying `y_A` by
   `exp(-j 2 pi nu_c t)`. Split `y_B` into K segments of length L with
   `nu_half * L / fs <= 0.25` (`nu_half` = half-width of the Doppler window) and
   `K >= 64`. Correlate each segment against the stretch of `y_A` that starts
   `lag_min` samples after the segment start and is `L + W - 1` samples long (`W` =
   number of lags in the delay window, zero-filled outside the capture) with an FFT of
   length at least `L + W - 1`, keeping the `W` valid lags; then FFT across segments
   (zero-padded to at least `max(4K, 256)`) for Doppler. Equal-length circular
   correlation of segments is not acceptable: the delay window is normally many times
   the segment length (default UHF geometry: 31 to 36 ms against 2.8 ms). Row `l` of
   the zero-filled surface holds only `overlap(l)` products and is scaled by
   `N_B / overlap(l)`, so the off-peak mean is the same on every row (one floor for
   unscaled rows under-read the SNR by 3.5 dB at `|l| = 0.35 N`). The surface spans
   each window plus a guard of 24 lags and 8 main-lobe widths (`fs / B`) in delay and
   64 bins (`16 / T`) in Doppler, so a peak just outside a window is recognised there
   and its skirt inside is not reported. The delay window plus guard must lie within
   half the capture, and each Doppler window inside `+/- fs / 2` spanning at most
   `fs / 2`, or `ValueError`. A half-lag statistic (adjacent lags summed) nominates
   candidates, since a peak midway between two lags loses up to 3.9 dB on the grid.
2. Fine search: Newton on the exact CAF in delay and Doppler (fractional delay by an
   FFT phase ramp, exact derivatives; at most 24 steps, each limited to 0.3 of a main
   lobe and halved up to 8 times until `|chi|` does not fall), bias under 5 % of the
   CRLB at 30 dB post-correlation SNR. The sum uses the largest FFT-friendly number of
   B samples symmetric about the midpoint of all B samples (as many dropped at one end
   as the lag forces at the other), so the peak sits at the section 5 epochs (A at
   `t_mid + TDOA`, B at `t_mid`) even with drifting delay and Doppler; summing every
   overlapping sample would move both epochs by `TDOA / 2`. The cost is `2 |lag| / N`
   of the samples instead of `|lag| / N`. `T` (section 8) is the number of samples in
   this sum over `fs`, reported as `CafResult.integration_s`.
3. Post-correlation SNR `rho`: peak power over the mean off-peak power of the surface
   for the same number of samples in the sum (so `rho = B T C`). The floor is the
   median of sampled cells over `ln 2` (chi-square(2)), away from all peaks and
   notches, shaped across Doppler by the product-noise response of the band filter
   (with a 300 Hz filter and a +/- 100 Hz window a flat floor sits 1 dB low at the
   window centre). A detection needs `10 log10(rho) >= caf.min_snr_db` (default 13).
4. One-sigma estimates from the normalised peak (section 8):
   `C_hat = |chi_peak|^2 / (sum |y_A|^2 * sum |y_B|^2)` over the samples in the sum,
   `sigma_tdoa = F sqrt((1 - C_hat) / (2 B T C_hat)) / beta`,
   `sigma_fdoa = sqrt((1 - C_hat) / (2 B T C_hat)) / T_e`, with
   `B_rms^2 = sum (f - f_c)^2 P(f) / sum P(f)`, `f_c = sum f P / sum P`,
   `P(f) = Re{Y_A(f) conj(Y_B(f)) exp(j 2 pi f tau_hat) exp(-j arg chi_peak)}` after
   Doppler alignment, summed over the band where `P` smoothed over `m >= 17` bins
   exceeds 3.5 times its standard deviation (taken from the imaginary part), widened by
   `m` and limited to the stretches holding a clearly significant value (5 sigma, or
   half the level of a signal filling the band). Clipping the smoothed `P` at zero
   instead lets noise-only bins inflate the bandwidth of a narrow signal (a CW read 2
   to 3 kHz). `F` is the RMS bandwidth about `f_c` of the incoherent part of the
   cross-spectrum (`Im{...}^2` of the same product) over `B_rms`: the data-driven `q`
   term of section 8, 1 when the signal fills the band; without it `sigma_tdoa` is 2.5
   times too small for a 10 kHz signal correlated over 31.25 kHz.
   The estimator also computes the 2x2 curvature `H` of `|chi|^2` at the peak in
   `(tau, nu)` and `rho_tf = H_tau,nu / sqrt(H_tau,tau H_nu,nu)`, the time-frequency
   coupling of the waveform (`1 / m` for `m` linear sweeps across the capture); it is
   the error correlation only when one channel is noise-free (with two noisy channels
   the errors correlate at 0.69 to 0.80 against `rho_tf = 0.48`). If `|rho_tf| > 0.9`
   the result is `tdoa = None`, `fdoa = None`, reason `"delay-doppler coupled"`.
   Otherwise both sigmas use the coupled variances of section 8 rather than a factor
   `1 / sqrt(1 - rho_tf^2)`, which is the one-noise-free-channel case and understates
   the scatter 1.12 to 1.34 times at `rho_tf = 0.48`. In them `rho_tf^2` is first reduced
   by its noise excess `max(F, 1)^2 / (2 rho)` (not below 0), and the signal-times-noise
   share is measured from a 32-block cross-spectrogram when the reduced coupling is at
   least 0.05. Reported sigmas are never below 2e-4 sample and `2e-4 / T` (the fine
   search's numerical accuracy; identical channels would give 0), and are `None`,
   never inf or NaN, when not detected or not finite.
   When the signal is too narrow for delay to be observable (`sigma_tdoa` above
   `caf.max_tdoa_sigma_s` or the delay cut is flat), return `tdoa = None`, keep the
   FDOA, reason `"narrowband"`. The cut is flat when the peak rises above the ridge it
   sits on (median of the delay cut less the noise) by less than 0.8 of the threshold:
   it is then a noise-lifted cell of a ridge, its `sigma_fdoa` follows the ridge level,
   and `rms_bandwidth_hz` is `None`.
5. Exclusion zones: the caller passes predicted self-peaks `(tdoa_s, fdoa_hz)` (the
   same satellite seen in both channels, known exactly from the downlink geometry;
   the oscillator term cancels). A notch covers the main lobe (first nulls) around
   each, and the whole delay window at that Doppler when the delay cut is flat. A
   main-lobe notch alone leaves the -13 dB sidelobes of a strong self-peak above a
   weaker true peak, so the predicted sidelobe envelope of a visible self-peak (its
   level measured on an exact delay cut through the predicted point, a linear
   correlation scaled like the surface, with a factor 2 margin) is subtracted in
   amplitude from nearby cells before thresholding. The same discount applies around
   every extracted peak, so a sidelobe is never a candidate: a second peak must exceed
   4 times a stronger peak's sidelobe envelope.
6. Mirror handling for co-channel cross-talk: the caller may pass a second admissible
   window pair for the swapped pairing (j via a against i via b). If a peak in that
   window, distinct from the primary (outside its main lobe) and reaching the
   threshold, is not more than 6 dB below the primary peak (a stronger one included),
   the result is flagged `mirror_ambiguous`; `tdoa` and `fdoa` are still returned and
   the mirror peak is appended to `candidates`.
7. Returns a patch of the surface around the peak (float32 dB over the noise floor,
   floored at -30 dB, at most 65 x 65) and the two 1-D cuts spanning the full windows,
   for display; a cut longer than 4096 points is reduced to the maximum of each group.
8. Ambiguity: after notching, if a second peak within 3 dB of the strongest, itself
   reaching `caf.min_snr_db`, lies inside the primary window and outside the strongest
   peak's main lobe, return `tdoa = None`, `fdoa = None`, `detected = True`, reason
   `"ambiguous"`, and list up to 8 peaks in `candidates`. (Without the threshold
   condition every detection within 3 dB of it would be ambiguous against noise peaks.
   A sawtooth chirp of 25 kHz and 10 ms period has peaks at 0.996 of the true one at
   +/- 40 us, +/- 100 Hz and 1.000 at +/- 10 ms.) The coupling test of item 4 comes
   first, because a single-sweep ridge otherwise reads as many peaks. Such results are
   never passed to the solver.

Comparisons between peaks (the main peak, the 3 dB and 6 dB rules) use raw `|chi|^2`,
not post-correlation SNR; peaks at distant lags then differ by up to twice the dB
difference in the samples summed (about 0.6 dB for 72 ms in a 1 s capture).
`CafResult.reason` is `None` for a complete result, otherwise one of `caf.REASON_*`:
`"below threshold"`, `"ambiguous"`, `"delay-doppler coupled"`, `"narrowband"` (FDOA
only) or `"peak outside window"` (the only cells above threshold are the skirt of a
peak outside the window).

**Search windows** are built by the caller from geometry. `geometry.measurement_windows`
returns a bound on the predicted values over the whole admissible area (inside the
region, both satellites at or above the mask), not the extremes at grid points, which
missed admissible emitters by up to 614 us at the default 2 degree step: the extremes
over the admissible grid points and their eight neighbours, the region's north and east
edges, 720 samples of each satellite's mask contour with the ends of every admissible
arc bracketed on the contour and, at an epoch whose co-visible area holds no grid point
(a lens between two low satellites), a refined grid over that area's bounding box; both
ends are padded by one eighth of the largest second differences along each axis.
`step_deg` is the largest step: it is cut to 1/8 of the lowest satellite's height (as an
angle at the Earth's centre) and halved until the region is at least 5 points across per
axis. The bound can be about one cell wider than the true extremes where a horizon
crosses the grid (2.3 ms on a 22 to 34 ms span for satellites 108 degrees apart). It is
`None` when neither a grid point nor a contour sample is admissible (a co-visible sliver
thinner than about 1e-3 degree lying between two contour samples is missed). It is
widened in delay by
`caf.delay_margin_s + 3 sqrt(clock_a^2 + clock_b^2 + tdoa_bias^2)` and in Doppler by
`caf.doppler_margin_hz + 3 sqrt((sat_osc T_i)^2 + (sat_osc T_j)^2 + (site_osc f_tune,a)^2 + (site_osc f_tune,b)^2)`,
except that terms common to both paths cancel as in the measurement and in the section 9
partials: one site on both channels gives no clock term and
`site_osc (f_tune,a - f_tune,b)` in place of the two site terms; one satellite on both
paths gives `sat_osc (T_i - T_j)` in place of the two satellite terms. When a
calibration of the pair newer than `calibration.max_age_s` exists, each kind it covers
is shifted by its correction and gets only its fixed margin (`search_windows(...,
correction=(tdoa_corr | None, fdoa_corr | None))` or `{"tdoa": ..., "fdoa": ...}`); a
kind without one keeps the full widening.

The CAF assumes the differential delay drifts by less than `0.5 / B` and the
differential Doppler by less than `1 / T` over the capture. The caller checks this
from predicted dynamics and shortens the capture or flags the result.

## 7. Detection and classification

- PSD frames: `navg` non-overlapping Hann segments of `nfft` samples read at
  `fs = span_hz`; power = mean `|FFT(w x)|^2 / sum(w^2)`, so unit-variance white noise
  reads 0 dB in every bin; fft-shifted; `t_ns` is the first-sample time.
- Baseline per bin: median over a history of at most 120 frames spanning
  `baseline_tau_s` (one frame kept every `max(baseline_tau_s / 120, 1 / frame_rate_hz)`
  s, capacity `min(120, baseline_tau_s / interval)` entries; a history spans
  `entries - 1` keep intervals). Bins in detection (above threshold in the kept frame,
  or inside a live track's extent plus one skirt bin on each side) neither gain nor
  lose entries. Until the history spans `baseline_tau_s / 10`, bins outside configured
  carriers use the prior `F + R_k`. `F` is the median of the current frame over guard
  bins (outside every transponder passband and configured carrier) when there are at
  least 8 of them, otherwise the 10th percentile of `psd - R_k` raised by the
  percentile-to-median offset of the frame's own fluctuation statistics (the raw
  percentile sits 2.2 dB under the median at `navg = 8`, 8.2 dB at `navg = 1`, and
  without removing `R_k` the relay rise counts twice). Either way bins in detection, or
  more than the threshold above the previous baseline, are left out and `F` is held
  when fewer than 16 usable bins remain (half the guard bins when there are fewer than
  32), so an emitter that appears during start-up cannot lift its own prior; `F` is
  averaged over 5 frames. `R_k = 10 log10(1 + N0_relay / N0_site)` inside a transponder
  passband (overlapping passbands add their relayed noise linearly;
  `linkbudget.relay_rise_db` gives it per channel and transponder), 0 elsewhere. Bins
  inside a configured carrier (its range widened by 2 bins, the Hann main lobe) are not
  tested until their own history spans `min(30 s, baseline_tau_s / 2)` (a history never
  spans more than `baseline_tau_s`). An emitter already on at start-up is therefore
  detected against the prior, and its bins stay frozen there.
- A signal is a run of contiguous bins (gaps up to `merge_bins`) above
  `baseline + T_eff`, confirmed when seen in M of the last N frames, released after
  `release_frames` misses; it keeps a stable `track_id`. `T_eff`
  (`SignalTracker.effective_threshold_db`) is `threshold_db` or, when larger, the level
  a noise bin exceeds with probability 1e-4 for the number of averages read from the
  frames' own bin-to-bin scatter (median absolute dB difference of bins 3 apart,
  averaged over 128 frames): 11.2 dB at `navg = 1`, 8.5 at 2, 6.4 at 4, under 6 dB from
  5 up. A literal 6 dB threshold is crossed by 6.3 % of noise bins per frame at
  `navg = 1`. The track's extent is the hull of its runs over the last `confirm_n`
  frames; `f_lo_hz` and `f_hi_hz` are its outer bin edges, and its values are measured
  over it in frames where it is seen and held otherwise (a detection stays listed, with
  its old `last_seen_ns`, for up to `release_frames - 1` missed frames). With `p` and
  `b` the linear power and baseline: `power_db = 10 log10(mean p)` and
  `snr_db = power_db - 10 log10(mean b)` over the extent,
  `cn0_dbhz = 10 log10(sum max(p_k - b_k, 0) / mean(b_k) * bin_hz)` with the sum over
  the extent plus one skirt bin on each side (a Hann window puts a third of a
  bin-centred tone's power in the two adjacent bins; the run alone reads up to 1.76 dB
  low). `b` is the median baseline, so `snr_db` and `cn0_dbhz` read high by the frames'
  median-to-mean ratio (0.18 dB at `navg = 8`).
- Track identity (revision 2 left it open). Unconfirmed fragments joined by one run
  merge at once, with the nearest confirmed track when the run touches several. Two
  confirmed tracks merge under the lower id only after sharing a run in `confirm_m`
  consecutive frames; until then each takes the part of the shared run on its side of
  the midpoint between them, so two detections never share a bin. A track is cut at a
  stretch of its extent that has had no bin above threshold for
  `release_frames + confirm_n` frames when the stretch is wider than
  `4 (merge_bins + 1)` bins, or wider than `merge_bins` and back at its baseline (mean
  power of the last `confirm_n` frames within `threshold_db / 4` of it). Each piece
  takes back the id it had before and a piece that never had one re-confirms; when the
  signal a merged track is named after ends, the one it absorbed carries on under its
  own id and first-seen time.
- A track overlapping a configured carrier (range widened by 2 bins) by at least half
  its own width carries `authorized_carrier_id` and is reported only while its power is
  more than `power_anomaly_db` above that carrier's baseline (`anomaly = true`). The
  power is the mean over the last `confirm_n` frames and over the bins it shares with
  the carrier (one frame's run is by selection already `threshold_db` above baseline),
  against the median baseline raised by the frames' mean-to-median ratio (an unchanged
  carrier would otherwise read +0.18 dB at `navg = 8`, +1.6 dB at `navg = 1`); "more
  than" carries a 1e-5 dB tolerance (float32 resolution), and the anomaly clears after
  `release_frames` consecutive frames at or below the limit.
- Uplink mapping: a detection maps to the uplink through the transponder (of a
  satellite the channel watches) whose downlink passband contains the detection
  centre, `f_up = f_down - translation_hz`. A detection outside every such passband
  has no mapping and forms a single-channel event that can only be local or
  undetermined.
- Event identity: detections on any channels and any satellites whose mapped uplink
  ranges overlap are one event. The event keeps one evidence row per channel whose
  monitor covers the mapped downlink: `seen`, `not_seen_capable`, `not_capable` or
  `stale`.
- Capability: a channel B is `not_seen_capable` for an event seen on channel A of the
  same satellite when its monitor covers the mapped downlink, its node is online with
  a PSD frame newer than 3 frame periods, it has no detection overlapping the range,
  and `cn0_A + q_B - q_A - 10 log10(max(f_hi - f_lo, bin_hz_B)) >= threshold_db + sensitivity_margin_db`,
  with `q = downlink_quality_db = G_site(off-axis toward the satellite) - FSPL(f_d, rho_d) - L_pol - 10 log10(N0_site + N0_relay)` (linear sum inside the log).
  Otherwise it is `not_capable` (or `stale` when the node is offline or its frame is
  old).
- Verdict, evaluated once `verdict_settle_s` has passed since the event's first
  confirmed detection, then on every change. `uplink`: seen at two sites farther apart
  than `min_site_separation_km` (same or different satellites), or tasking reports a
  cross-satellite CAF detection inside the primary window. `local`: all `seen` rows
  lie within `min_site_separation_km` of one site and some row at a farther site is
  `not_seen_capable`. `undetermined`: otherwise.
- Classification from a short IQ capture: occupied bandwidth, spectral flatness,
  kurtosis of the envelope, linearity of instantaneous frequency (sweep rate),
  cyclostationary symbol-rate line, on/off duty cycle. Classes: `cw`, `swept`,
  `noise`, `modulated`, `pulsed`, `unknown`, with a confidence. `classify` computes its
  own Welch PSD with 75 %-overlapping Hann segments (same normalisation; with
  non-overlapping segments a sweep whose period is near the segment length shows the
  window shape), rejects captures shorter than 1024 samples and looks at the first
  262144; `unknown` carries confidence 0 and every feature is a finite float.

## 8. Stein bounds

`B` = the bandwidth the two channels are correlated over (the section 6 band filter;
the capture rate only when the signal fills it), integration `T`, input SNRs `g1`,
`g2` in `B`, effective `1/g = 0.5 (1/g1 + 1/g2 + 1/(g1 g2))`:

```
sigma_tdoa = 1 / (beta sqrt(B T g))        beta = 2 pi * B_rms
sigma_fdoa = 1 / (T_e  sqrt(B T g))        T_e  = 2 pi * T_rms = pi T / sqrt(3)   (rectangular window)

C    = g1 g2 / ((1 + g1)(1 + g2))          magnitude-squared coherence in B
rho  = B T C                               post-correlation SNR (section 6 item 3); -> B T g / 2 for g1, g2 << 1
B T g = 2 B T C / (1 - C)
```

For a flat spectrum of width `B_s`, `B_rms = B_s / sqrt(12)`. Correlated over a band
wider than the signal, `sigma_tdoa = sqrt((1/g1 + 1/g2 + q/(g1 g2)) / (2 B T)) / beta`
with `q = B^2 / (12 B_rms^2) >= 1`; `sigma_fdoa` is unchanged. (Measured: a signal
filling 0.8 of the band has 1.22 times the plain formula's scatter.) `stein_sigmas`
clamps `q` at 1 and returns `sigma_tdoa = inf` for `B_rms = 0` (a CW).

Delay-Doppler coupling. The peak errs by `M^-1 g`, `M` the curvature of the peak,
proportional to `[[beta^2, -rho beta T_e], [-rho beta T_e, T_e^2]]` (`rho` = `rho_tf` of
section 6). The signal-times-noise part of the gradient `g` has covariance proportional
to `M`; the noise-times-noise part carries none of the signal's coupling. With
`a = 1/g1 + 1/g2`, `b = 1/(g1 g2)` and `k = 1 / (1 - rho^2)`:

```
var(tau) = [ (a + q b) k + rho^2 (1 + q) b k^2 ] / (2 B T beta^2)
var(nu)  = [ (a +   b) k + rho^2 (1 + q) b k^2 ] / (2 B T T_e^2)
```

(`crlb.stein_sigmas(..., rho_tf=)`). From a measured coherence
(`crlb.sigmas_from_coherence(C, B, B_rms, T, noise_rms_bw_hz=None, *, rho_tf=,
signal_noise_share=)`) the code uses `(1 - C) / (2 B T C)` times
`F^2 k + rho^2 E k^2` (over `beta^2`) and `k + rho^2 E k^2` (over `T_e^2`),
`E = 2 (1 - s) + max(F^2 - 1, 0)`, with `F` the
section 6 ratio (1 when `noise_rms_bw_hz` is not given) and `s = a / (a + b)` the
signal-times-noise share. `s = 1` (one noise-free channel) gives revision 2's
`1 / sqrt(1 - rho^2)` rule; when `s` is not given its smallest value
`2 sqrt(C) / (1 + sqrt(C))` (equal SNRs, the largest sigmas for that coherence) is used.
Measured with a two-sweep 20 kHz sawtooth (`rho = 0.48`) over 31.25 kHz, 300 trials at
equal SNRs of -3 and -8 dB: the scatter is 0.97 to 1.01 times these formulas and 1.12
to 1.34 times the `1 / sqrt(1 - rho^2)` rule.

## 9. Position solver

Unknown: emitter geodetic latitude and longitude at fixed height `h0`
(`solver.height_m`, default 0). Rows `z_k` (TDOA or FDOA, any mix of pairs, sites and
epochs) with CAF noise `sigma_k` and optional calibration correction `c_k`.

```
R = diag(sigma_k^2)                 CAF noise only
S = R + H_c S_c H_c^T               total measurement covariance (with uncertainty None,
                                    H_c holds only the reference-measurement columns)
minimise  r^T S^-1 r,   r_k = z_k - c_k - h_k(lat, lon)
```

- Minimise with `scipy.optimize.least_squares`, method `lm`, at most 100 evaluations.
  It is handed `S^-1/2 r`, the symmetric square root built through the small Gram
  matrix of the consider columns, not the Cholesky `L^-1 r` (`S = L L^T`): the two
  differ by an orthogonal matrix, so cost, minimiser, chi2 and `J^T S^-1 J` are the
  same, and no `m x m` factorisation is needed per start (a 400 x 400 Cholesky took up
  to 340 ms under load). `Solution.residuals` are `L^-1 r`. The Jacobian is numeric but
  not scipy's: central differences over +/- 1 km in the same vectorised predictor call
  (scipy's 0.2 m step is swamped by the FDOA predictor's 1e-8 Hz rounding noise), the
  variables being metres east and north of the start. `H_c` is evaluated once at the
  centroid of the admissible grid cells for the grid stage, at each start for
  refinement, and again at the converged point followed by one more refinement.
  Weighting by total covariance matters: with noise-only weights an FDOA row carrying
  an uncalibrated oscillator offset makes the fix worse than TDOA alone (measured
  74 km against 17 km).
- Consider parameters, all constant over the rows of one solve: per-satellite position
  (3, RIC) and velocity (3, RIC) through `PerturbedOrbit` with `t_ref` = the row's
  epoch (one-sided differences of 1 km and 0.02 m/s; the velocity partial is taken
  with one perturbed orbit per parameter referred to a common `t_0`, the row epoch
  nearest the mean, plus `-(t_k - t_0) dh_k/dpos`, equal to the row-epoch definition
  to first order); per-site clock offset (TDOA partial -1 for site a, +1 for site b,
  zero when a = b); per-site oscillator error (FDOA partial `-f_tune,A` for site a,
  `+f_tune,B` for site b); per-satellite oscillator error (FDOA partial `+T_nom,i`,
  `-T_nom,j`); per-pair TDOA bias (partial 1, sign following the orientation of the
  pair); one parameter per distinct reference measurement (below).
- Reference-emitter calibration (differential): a measurement `z_ref` of a known
  emitter at `x_ref` on the same pair (same satellites, sites and transponders; its
  own `f_e`), taken within `calibration.max_age_s`, gives `c = z_ref - h_ref(x_ref)`.
  The row is `z - c - h(x)`. Its consider partials are `H_c(x) - H_c,ref(x_ref)` for
  every parameter constant between the two measurements (satellite position, velocity
  and oscillator; site clocks and oscillators; pair bias), with `H_c,ref` evaluated at
  the reference's `f_e` and epoch. Through the same two sites the clock and pair-bias
  terms cancel exactly; the site-oscillator terms leave
  `(f_tune,target - f_tune,ref) eps_site`, because the reference is tuned to its own
  carrier, and the code carries that exact difference. The noise of `z_ref` is **not**
  added to `sigma_k`: each reference measurement is one more consider parameter with
  variance `sigma_ref^2` and partial 1 on every row that uses it, also when
  `uncertainty` is `None` (several target rows normally share one reference
  measurement, so its noise is common-mode; a shared-reference Monte Carlo gives NEES
  2.00 with `P_total` and 56 with `P_noise`). `solver.calibration_correction` returns
  `c` and the `CalRef`; reference parameters are keyed by `(cal_id, kind)`. A target
  row on the reversed pair uses `-c` with the same `CalRef` (recognised from the paths;
  partials and reference column change sign); a reference on other satellites, a
  reference row that itself carries a correction, and one `cal_id` naming two
  different measurements raise `ValueError`.
- Start. `solve(..., starts=None)` runs the global search: cost on a grid of step
  `solver.grid_step_deg` over the admissible region (points inside the region of
  interest with every satellite used above `solver.min_elevation_deg`), using only the
  newest 2 rows of each (pair, kind). Starts are the admissible cells whose cost is
  not greater than that of any of their 8 neighbours, the `solver.n_starts` (default
  16) lowest of them. With `starts` given (the pipeline passes the previous solution
  and its ambiguities) the grid is skipped. In both cases the equatorial mirror
  `(-lat, lon)` of each refined minimum is refined too, admissible or not: an
  inadmissible mirror that is never refined cannot be known to be a minimum, and item
  12.9 needs `region` for it. Refine each start with all rows. A converged point that
  fails the observability rule (below) is refined again from 10 km either side along
  its weakest axis (with mirrors) and dropped if that reaches a chi2 lower by more than
  1e-6: it was a saddle (on the equator under equatorial satellites every row is
  symmetric in latitude, so LM started there stops where `J` has no north component,
  while the minima lie north and south). A refinement whose last 5 LM evaluations all
  fail the observability rule is abandoned (it would creep along a flat valley for
  seconds). Discard a refined point if LM did not converge, if it is outside the region
  of interest, or if any satellite used is below the elevation mask there (at each
  row's own epoch; exactly on the mask counts as above). Points within 1 km (geodesic)
  of a lower-cost point are duplicates.
  Order candidates by chi2, ties within 1e-6 by descending latitude; the first is the
  solution and the others with `chi2 - chi2_best <= solver.chi2_ambiguity` are its
  `ambiguities`. The pipeline runs the global search on an event's first solve, every
  10th solve, and whenever a warm-started solve has `reduced_chi2 > 3`.
- Residuals are evaluated in batches per (kind, a, b), vectorised over row epochs and
  grid points; scalar predictor calls in the grid stage are too slow.
- Mirrors. Two rows generally intersect twice on the ellipsoid. For satellites in the
  equatorial plane the second TDOA intersection is the exact north/south mirror with
  identical chi2 for any number of TDOAs. A geostationary satellite with zero ECEF
  velocity gives FDOA rows that do not depend on the emitter at all, so FDOA separates
  the mirrors only when at least one satellite has north-south velocity, and TDOA
  separates them only when a satellite is off the equatorial plane and there are at
  least 3 rows; otherwise only the region of interest does.
  `Solution.ambiguity_resolved_by`, in order of precedence: `unresolved`
  (`ambiguities` is non-empty), `region` (a discarded inadmissible minimum lies within
  `chi2_ambiguity` of the solution), `measurements` (some other minimum, admissible or
  not, was found beyond that gate), otherwise `none`. (Read literally, revision 2 would
  call a far inadmissible minimum with chi2 near 1e6 `region`.)
- Covariance. East/north metres: `dE = (N(lat) + h) cos(lat) dlon`,
  `dN = (M(lat) + h) dlat` with the WGS84 prime-vertical and meridional radii; `J` by
  central differences with 100 m steps; matrices ordered `[east, north]`.
  `P_total = (J^T S^-1 J)^-1`, `K = P_total J^T S^-1`, `P_noise = K R K^T`,
  `P_consider = K H_c S_c H_c^T K^T` (they sum to `P_total`).
- Ellipse: semi-axes `= sqrt(-2 ln(1 - p)) * sqrt(eigenvalues of P_total)`;
  `azimuth_deg` = bearing of the semi-major axis clockwise from true north, in
  `[0, 180)`. CEP50 = the radius `r` with `P(|e| <= r) = 0.5` for `e ~ N(0, P_total)`:
  solve `(2 / (pi s1 s2)) * integral over theta in [0, pi/2] of (1 - exp(-r^2 a(theta) / 2)) / a(theta) = 0.5`,
  `a(theta) = cos^2(theta) / s1^2 + sin^2(theta) / s2^2` (the usual shortcuts are off
  by a factor of 2 for elongated ellipses).
- Goodness of fit: `chi2 = r^T S^-1 r` (this is 2 x scipy's `cost`), `dof = m - 2`,
  `reduced_chi2 = chi2 / dof` when `dof >= 1`, otherwise `None` (never NaN). With
  `m = 2` every admissible intersection has chi2 = 0 and all are returned. `m < 2`
  raises `ValueError`. If `J^T S^-1 J` is not positive definite, or the 1-sigma
  semi-major from `P_total` exceeds 1e7 m, at the solution or at one of its ambiguities
  (a place the rows cannot pin down that fits as well as the fix), `solve` returns
  `(None, "unobservable")`; so it does when no start converged and some were abandoned
  as flat. The other reasons are `"no admissible region"` (no grid cell inside the
  region with every satellite above the mask), `"no admissible solution"` (every
  converged point inadmissible) and `"not converged"`.
- `dominant_error` is the largest contributor to the trace of `P_total` among
  `noise`, `ephemeris_position`, `ephemeris_velocity`, `clock`, `oscillator`,
  `tdoa_bias`, `reference`.
- Stationary emitter assumed; multi-epoch fusion is just more rows.

## 10. Accuracy prediction

For a candidate emitter position and an `EmitterModel` (EIRP, antenna, pointing,
bandwidth, waveform), compute path budgets, input SNRs, Stein sigmas, `J`, `P_total`
and report the 95 % semi-major axis, CEP50 and the dominant term. The measurement set
is one TDOA and one FDOA per listed pair at `t_ns` (TDOA omitted when its predicted
sigma exceeds `caf.max_tdoa_sigma_s`; a pair whose predicted post-correlation SNR
`B T C` is below `caf.min_snr_db` contributes nothing). With a reference carrier,
every row is differenced against that carrier on the same transponders, and the
reference measurement's sigma is predicted from the carrier's own `EmitterModel`.
Cells with fewer than two rows, or where the observability rule of section 9 fails,
hold null with reason `"unobservable"`. Evaluated over a grid this is the planning map.
Predicted sigmas and SNRs use the `B` and `T` the estimator would use
(`CafResult.noise_bandwidth_hz`, `CafResult.integration_s`), with `g1`, `g2` in that
`B`. `solver.position_covariance` gives `P_total`, its noise and consider parts, the
ellipse, CEP50, the dominant term and the per-source budget at a given position with
the `J`, `S` and observability rule of `solve` (row values unused), or `None` when
unobservable.

## 11. Lines of position

For display: the zero contour of `h(lat, lon; height_m) - (z - c)` over a grid,
evaluated only at cells where both satellites are at or above the elevation mask at
the row's epoch (other cells NaN), and the band between the contours at `-k sigma` and
`+k sigma`, via `contourpy`, returned as a GeoJSON FeatureCollection of at most two
features: `role` `line` (MultiLineString) and `band` (MultiPolygon, exterior rings
counter-clockwise); a feature with no geometry is omitted. For a calibrated row the
band uses `sqrt(sigma^2 + sigma_ref^2)`, since `z - c` carries both noises; the
properties carry both (`sigma`, `sigma_total`).

Longitudes of gridded and GeoJSON outputs follow `Region.grid`: values in
`[lon_min, lon_min + 360)`, increasing, never jumping by more than 180 degrees between
consecutive vertices; consumers wrap for display. The contour grid adds the region's
north and east edges when the step misses them and, for the full circle, repeats the
first meridian 360 degrees east so contours cross the seam; coordinates are rounded to
1e-6 degree and seam vertices held just below `lon_min + 360`. Point outputs are
normalised to `[-180, 180)`.

## 12. Sanity tests every implementation must pass

1. Geostationary satellite, zero inclination: ECEF position constant to 1 mm, ECEF
   velocity zero; inclined 5 degrees: z amplitude `a sin(5 deg)`, peak ECEF speed
   about `a * 0.0873 * OMEGA_E` (268 m/s). A geostationary TLE returns its catalogued
   sub-satellite longitude. The residual of a degree-5 fit to site range over 0.5 s
   sampled at 31.25 kHz is below 1e-6 m for every orbit type; for a `tle` in low orbit
   only within about 5 days of its element epoch. SGP4 keeps time as float64 minutes
   since that epoch, and that granularity times the range rate grows with element age
   (measured: 9e-7 m at 5 days, 3.4e-6 m at 11 days, 1.1e-5 m at 60 days);
   geosynchronous TLEs stay below 1e-6 m at 90 days.
2. Emitter on the equator at height 0 exactly under a zero-inclination `geo`
   satellite: `tau_u = (a - WGS84_A) / C = 35 786 035.37 m / C = 0.119369365075 s`,
   within 1 mm of range (3e-12 s).
3. Sagnac. Uplink, emitter on the equator 60 degrees east of a zero-inclination GEO
   satellite: `uplink_delay - |r - x_e| / C` equals `OMEGA_E (x_e x r)_z / C^2`
   (= -1.88964e-7 s) within 1 ps (the true second-order residual is 0.66 ps; do not
   tighten). Downlink, site at the same point: `downlink_delay - |x_g - r| / C` equals
   `-OMEGA_E (x_g x r)_z / C^2` (= +1.88964e-7 s) within 1 ps.
4. `bent_pipe_rates` equals a central difference of `bent_pipe` to 1e-13 s/s, for
   `geo`, `tle` and `kepler` orbits.
5. Simulator round trip: an IQ capture of a noise emitter through two satellites,
   estimated by the CAF, returns TDOA and FDOA equal to the section 5 predictions
   within 3 sigma of the Stein bound; swapping A and B flips both signs to within a
   fraction of a sigma (the section 5 definition is not antisymmetric, and the fine
   sum's samples follow channel B, section 6 item 2); re-rendering with A starting
   0.38 ms late, or B starting 0.52 ms late, leaves `tdoa_s` unchanged.
6. A positive clock offset `delta_a` lowers `TDOA_AB` by `delta_a`. Raising the
   satellite oscillator error of satellite i by `eps` raises `FDOA_AB` by
   `eps * T_nom,i`.
7. Noise waveform: the normalised autocorrelation of 64 consecutive raw blocks is
   below 0.02 at every non-zero lag (in particular 4094 and 8188 grid samples); for
   random `b` near 1.79e18, `evaluate(b, t)` equals
   `evaluate(b + 123_456_789, t - 0.123456789)` to 1e-9 relative while
   `bandwidth_hz * |t_rel|` is at most about 1e6 (a 1 s capture at 1 MHz; the tests use
   25 and 300 kHz). Beyond that 1e-9 cannot hold: the float64 rounding of
   `t_rel * fs_base_hz` and of the caller's own `t - 0.123456789` moves the instant by
   about 2e-16 `|t_rel|`, a relative error of about 1e-15 `bandwidth_hz * |t_rel|`
   (measured 4.4e-10 at 1 MHz and 4.9e-9 at 5 MHz, both at `|t_rel|` = 1 s; about
   -165 dB, physically irrelevant).
8. Solver. Three `geo` satellites: `lon_deg` -177 / 172 / 75, `inclination_deg`
   5 / 4 / 5, `phase_deg` 40 / 200 / 310; `t` = epoch + 3 h; emitter at 30.37 N,
   140.41 E at `solver.height_m` with antenna height 0; rows: TDOA and FDOA of pairs
   (sat1, sat2) and (sat1, sat3), `f_e` = 300 MHz, sigma 1 us and 0.05 Hz; default
   region; uncertainty = None. (a) Noise-free: solution within 1 m, chi2 < 1e-6, no
   ambiguities. (b) 500 seeded trials of Gaussian noise with those sigmas:
   NEES = `e^T P^-1 e` with `e` = estimate minus truth in east/north metres and `P` =
   that trial's `cov_noise_en_m2`; mean NEES in [1.73, 2.27]; fraction with
   NEES <= 5.991 in [0.92, 0.98]; mean chi2 in [1.73, 2.27] (dof 2).
9. Mirror. Three `geo` satellites with inclination 0 at -177 / 172 / 75, the two TDOA
   rows only, noise-free, emitter at 30 N 140 E, default region: the solution plus its
   `ambiguities` are exactly two points, (30, 140) and (-30, 140), each within 1 m,
   chi2 difference < 1e-6, `ambiguity_resolved_by = unresolved`. Adding the two FDOA
   rows changes nothing. With region `lat_min = 0` only (30, 140) is returned and
   `ambiguity_resolved_by = region`.
10. Consider covariance: with a satellite oscillator bias drawn from its configured
    sigma in every trial, NEES computed with `P_total` averages about 2 while NEES
    with `P_noise` is far larger, on rows where the biased FDOAs carry the fix: the
    item 8 geometry with TDOA(sat1, sat2), FDOA(sat1, sat2) and FDOA(sat1, sat3),
    0.005 Hz FDOA sigma, `sat_osc_frac` 1e-9 (measured 2.1 against 65). On the item 8
    row set it cannot hold: total-covariance weighting routes around the biased FDOA
    rows, and the expected NEES with `P_noise`, `trace(P_noise^-1 P_total)`, is 2.000
    to 2.005 for oscillator sigmas from 1e-6 down to 1e-9.
