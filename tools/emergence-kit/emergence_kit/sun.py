"""Sunrise and sunset, offline, from the NOAA solar calculator equations.

This is the Julian-century method NOAA's own spreadsheet uses (Meeus-based), iterated
at the event time. It agrees with published tables to about a minute across the UK;
the simpler fractional-year formula is up to ~3 minutes out in the south-west, which
matters when a report states minutes after sunset.

Zenith 90.833 deg: the sun's upper limb on the horizon, with standard refraction.
Returns None for polar day / polar night.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone


def _solar(jd: float) -> tuple[float, float]:
    """(declination deg, equation of time minutes) at Julian day `jd`."""
    t = (jd - 2451545.0) / 36525.0
    l0 = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360
    m = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    mr = math.radians(m)
    c = (math.sin(mr) * (1.914602 - t * (0.004817 + 0.000014 * t))
         + math.sin(2 * mr) * (0.019993 - 0.000101 * t) + math.sin(3 * mr) * 0.000289)
    omega = math.radians(125.04 - 1934.136 * t)
    lam = math.radians(l0 + c - 0.00569 - 0.00478 * math.sin(omega))
    eps0 = 23 + (26 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60) / 60
    eps = math.radians(eps0 + 0.00256 * math.cos(omega))
    decl = math.degrees(math.asin(math.sin(eps) * math.sin(lam)))
    y = math.tan(eps / 2) ** 2
    l0r = math.radians(l0)
    eqt = 4 * math.degrees(y * math.sin(2 * l0r) - 2 * e * math.sin(mr)
                           + 4 * e * y * math.sin(mr) * math.cos(2 * l0r)
                           - 0.5 * y * y * math.sin(4 * l0r) - 1.25 * e * e * math.sin(2 * mr))
    return decl, eqt


def _event_minutes_utc(day: date, lat: float, lon: float, rising: bool) -> float | None:
    jd0 = (day - date(2000, 1, 1)).days + 2451544.5          # 0h UT on `day`
    minutes = 720.0
    for _ in range(3):                                        # converge on the event time
        decl, eqt = _solar(jd0 + minutes / 1440)
        phi, d = math.radians(lat), math.radians(decl)
        cos_ha = (math.cos(math.radians(90.833)) / (math.cos(phi) * math.cos(d))
                  - math.tan(phi) * math.tan(d))
        if not -1 <= cos_ha <= 1:
            return None
        ha = math.degrees(math.acos(cos_ha))
        noon = 720 - 4 * lon - eqt
        minutes = noon - 4 * ha if rising else noon + 4 * ha
    return minutes


def sun_event(day: date, lat: float, lon: float, rising: bool) -> datetime | None:
    """UTC datetime of sunrise (rising=True) or sunset on `day`. lon: + east, - west."""
    mins = _event_minutes_utc(day, lat, lon, rising)
    if mins is None:
        return None
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(minutes=mins)


def sunset(day: date, lat: float, lon: float) -> datetime | None:
    return sun_event(day, lat, lon, rising=False)


def sunrise(day: date, lat: float, lon: float) -> datetime | None:
    return sun_event(day, lat, lon, rising=True)
