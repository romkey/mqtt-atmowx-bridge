"""Values a station usually reports but a bare MQTT feed often does not.

These are opt-in: the bridge publishes what it receives, and only fills in a
derived field when the config asks for it and the field was not measured.
"""

from __future__ import annotations

import math


def dew_point_celsius(temperature_c: float, relative_humidity_percent: float) -> float:
    """Dew point from temperature and relative humidity.

    Magnus-Tetens with the WMO (2008) coefficients. Valid roughly -45..60 °C.
    """
    b = 17.62
    c = 243.12
    humidity = min(max(relative_humidity_percent, 0.1), 100.0)
    gamma = math.log(humidity / 100.0) + (b * temperature_c) / (c + temperature_c)
    return (c * gamma) / (b - gamma)


def sea_level_pressure_hpa(
    station_pressure_hpa: float, elevation_meters: float, temperature_c: float
) -> float:
    """Reduce absolute station pressure to mean sea level.

    The barometric formula with the ICAO standard lapse rate — the same
    reduction consumer weather stations apply.
    """
    lapse = 0.0065
    denominator = temperature_c + lapse * elevation_meters + 273.15
    return float(station_pressure_hpa * (1 - (lapse * elevation_meters) / denominator) ** -5.257)


def station_pressure_hpa(
    sea_level_hpa: float, elevation_meters: float, temperature_c: float
) -> float:
    """The inverse of :func:`sea_level_pressure_hpa`, for stations that only report MSLP."""
    lapse = 0.0065
    denominator = temperature_c + lapse * elevation_meters + 273.15
    return float(sea_level_hpa * (1 - (lapse * elevation_meters) / denominator) ** 5.257)
