"""Physical constants and fixed conventions (MATH.md section 1). SI units."""
from __future__ import annotations

C: float = 299_792_458.0
"""Speed of light, m/s."""

OMEGA_E: float = 7.2921151467e-5
"""Earth rotation rate used for light-time (Sagnac) and the `geo` orbit, rad/s."""

WGS84_A: float = 6378137.0
"""WGS84 semi-major axis, m."""

WGS84_F: float = 1.0 / 298.257223563
"""WGS84 flattening."""

WGS84_E2: float = WGS84_F * (2.0 - WGS84_F)
"""WGS84 first eccentricity squared."""

GM: float = 3.986004418e14
"""Earth gravitational parameter, m^3/s^2."""

J2: float = 1.08262668e-3
"""Second zonal harmonic (secular terms of the `kepler` orbit)."""

K_DBW: float = -228.6
"""Boltzmann constant, dBW/K/Hz."""

R_E: float = 6371e3
"""Mean Earth radius for the ionospheric mapping function, m."""

IONO_SHELL_HEIGHT_M: float = 350e3
"""Thin-shell ionosphere height, m."""

T_REF_S: float = 1767225600.0
"""Reference epoch for the linear Earth rotation angle: 2026-01-01T00:00:00Z, Unix s."""

GMST_REF: float = 1.756863409365046
"""GMST at T_REF_S, rad (sgp4.propagation.gstime(2461041.5))."""

OMEGA_GMST: float = 7.2921158554e-5
"""Rate of GMST, rad/s."""

JD_UNIX_EPOCH: float = 2440587.5
"""Julian date of 1970-01-01T00:00:00Z."""
