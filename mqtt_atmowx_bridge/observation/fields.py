"""The measurement fields of net.atmowx.observation, keyed by the dotted path
they occupy in the record.

Everything the bridge knows how to publish is declared here: its dimension
(which decides what units may be converted into it), how many decimals to keep,
and the range a plausible reading falls in. Anything not in this table has to go
through ``extra`` as a net.atmowx.defs#measurement.
"""

from __future__ import annotations

from dataclasses import dataclass

from .units import Dimension


@dataclass(frozen=True, slots=True)
class FieldSpec:
    #: Dotted path within the observation record, e.g. ``wind.speed``.
    path: str
    dimension: Dimension
    #: ``quantity`` fields encode as scaled integers; ``integer`` fields are bare integers.
    encoding: str
    #: Decimal places kept by default; overridable per mapping.
    decimals: int
    description: str
    #: Readings outside this range are rejected as sensor faults.
    minimum: float | None = None
    maximum: float | None = None
    #: Angles wrap into [0, 360) instead of being rejected.
    wraps: bool = False


def _quantity(
    path: str,
    dimension: Dimension,
    decimals: int,
    description: str,
    minimum: float | None = None,
    maximum: float | None = None,
) -> FieldSpec:
    return FieldSpec(
        path=path,
        dimension=dimension,
        encoding="quantity",
        decimals=decimals,
        description=description,
        minimum=minimum,
        maximum=maximum,
    )


#: Declaration order is the order fields appear in a published record.
FIELDS: dict[str, FieldSpec] = {
    "temperature": _quantity("temperature", "temperature", 1, "Air temperature, °C", -90, 60),
    "dewPoint": _quantity("dewPoint", "temperature", 1, "Dew point, °C", -90, 60),
    "relativeHumidity": _quantity("relativeHumidity", "ratio", 0, "Relative humidity, %", 0, 100),
    "pressureStation": _quantity(
        "pressureStation", "pressure", 1, "Absolute station pressure, hPa", 300, 1100
    ),
    "pressureSeaLevel": _quantity(
        "pressureSeaLevel", "pressure", 1, "Mean-sea-level reduced pressure, hPa", 850, 1100
    ),
    "wind.speed": _quantity("wind.speed", "speed", 1, "Wind speed, m/s", 0, 150),
    "wind.gust": _quantity("wind.gust", "speed", 1, "Wind gust, m/s", 0, 150),
    "wind.direction": FieldSpec(
        path="wind.direction",
        dimension="angle",
        encoding="integer",
        decimals=0,
        description="Wind direction, degrees true",
        wraps=True,
    ),
    "wind.gustDirection": FieldSpec(
        path="wind.gustDirection",
        dimension="angle",
        encoding="integer",
        decimals=0,
        description="Gust direction, degrees true",
        wraps=True,
    ),
    "precipitation.rate": _quantity(
        "precipitation.rate", "precipitationRate", 1, "Precipitation rate, mm/h", 0, 2000
    ),
    "precipitation.hour": _quantity(
        "precipitation.hour", "length", 1, "Precipitation in the last 60 minutes, mm", 0, 2000
    ),
    "precipitation.day": _quantity(
        "precipitation.day", "length", 1, "Precipitation since station-local midnight, mm", 0, 5000
    ),
    "precipitation.event": _quantity(
        "precipitation.event", "length", 1, "Precipitation in the current rain event, mm", 0, 5000
    ),
    "solarIrradiance": _quantity(
        "solarIrradiance", "irradiance", 0, "Solar irradiance, W/m²", 0, 2000
    ),
    "uvIndex": _quantity("uvIndex", "dimensionless", 0, "UV index", 0, 20),
    "visibility": _quantity("visibility", "distance", 0, "Visibility, m", 0, 100000),
    "snowDepth": _quantity("snowDepth", "length", 0, "Snow depth, mm", 0, 20000),
    "soilTemperature": _quantity(
        "soilTemperature", "temperature", 1, "Soil temperature, °C", -50, 80
    ),
    "soilMoisture": _quantity("soilMoisture", "ratio", 0, "Soil moisture, %", 0, 100),
    "presentWeather": FieldSpec(
        path="presentWeather",
        dimension="dimensionless",
        encoding="integer",
        decimals=0,
        description="WMO code table 4680 present weather",
        minimum=0,
        maximum=99,
    ),
    "airQuality.pm1": _quantity("airQuality.pm1", "concentration", 1, "PM1, µg/m³", 0, 10000),
    "airQuality.pm25": _quantity("airQuality.pm25", "concentration", 1, "PM2.5, µg/m³", 0, 10000),
    "airQuality.pm10": _quantity("airQuality.pm10", "concentration", 1, "PM10, µg/m³", 0, 10000),
    "airQuality.ozone": _quantity("airQuality.ozone", "concentration", 1, "Ozone, µg/m³", 0, 10000),
    "airQuality.no2": _quantity("airQuality.no2", "concentration", 1, "NO₂, µg/m³", 0, 10000),
    "airQuality.so2": _quantity("airQuality.so2", "concentration", 1, "SO₂, µg/m³", 0, 10000),
    "airQuality.co": _quantity("airQuality.co", "concentration", 1, "CO, µg/m³", 0, 100000),
}

FIELD_NAMES: list[str] = list(FIELDS)

#: ``extra`` entries are named ``extra:leafWetness`` in config.
EXTRA_PREFIX = "extra:"


@dataclass(frozen=True, slots=True)
class CoreTarget:
    field: str
    kind: str = "core"


@dataclass(frozen=True, slots=True)
class ExtraTarget:
    parameter: str
    kind: str = "extra"


Target = CoreTarget | ExtraTarget


def is_field_name(name: str) -> bool:
    return name in FIELDS


def field_spec(name: str) -> FieldSpec:
    return FIELDS[name]


def parse_target(name: str) -> Target:
    """Resolve a config ``field:`` value to a core field or an extra measurement."""
    if name.startswith(EXTRA_PREFIX):
        parameter = name[len(EXTRA_PREFIX) :].strip()
        if not parameter:
            raise ValueError(f'"{name}" is missing a parameter name after "{EXTRA_PREFIX}"')
        if len(parameter.encode("utf-8")) > 128:
            raise ValueError(f"extra parameter name too long: {parameter}")
        return ExtraTarget(parameter=parameter)

    if name not in FIELDS:
        raise ValueError(
            f'unknown observation field "{name}"; expected one of {", ".join(FIELD_NAMES)} '
            f'or "{EXTRA_PREFIX}<parameter>" for anything the lexicon does not cover'
        )
    return CoreTarget(field=name)
