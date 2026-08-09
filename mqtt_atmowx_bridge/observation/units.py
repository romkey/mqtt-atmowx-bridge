"""Unit conversion into the SI units the net.atmowx lexicon requires.

Every measurement is declared with a dimension, and each dimension has one
canonical SI unit. Conversions are only ever attempted within a dimension, so a
config that feeds inches of mercury into a wind speed is rejected at load time
rather than publishing nonsense.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Literal, get_args

Dimension = Literal[
    "temperature",
    "pressure",
    "speed",
    "length",
    "precipitationRate",
    "distance",
    "ratio",
    "irradiance",
    "angle",
    "concentration",
    "dimensionless",
]

DIMENSIONS: tuple[Dimension, ...] = get_args(Dimension)

#: The SI unit each dimension is published in, named as in
#: net.atmowx.defs#measurement. ``dimensionless`` still needs a name because
#: that def requires a unit string even for bare indexes.
SI_UNIT: dict[Dimension, str] = {
    "temperature": "celsius",
    "pressure": "hectopascal",
    "speed": "metersPerSecond",
    "length": "millimeters",
    "precipitationRate": "millimetersPerHour",
    "distance": "meters",
    "ratio": "percent",
    "irradiance": "wattsPerSquareMeter",
    "angle": "degrees",
    "concentration": "microgramsPerCubicMeter",
    "dimensionless": "index",
}

#: Multiply-by-this-to-get-SI tables. Keys are normalized (see ``normalize_unit``).
FACTORS: dict[Dimension, dict[str, float]] = {
    "pressure": {
        "hectopascal": 1.0,
        "hectopascals": 1.0,
        "hpa": 1.0,
        "millibar": 1.0,
        "millibars": 1.0,
        "mbar": 1.0,
        "mb": 1.0,
        "pascal": 0.01,
        "pascals": 0.01,
        "pa": 0.01,
        "kilopascal": 10.0,
        "kilopascals": 10.0,
        "kpa": 10.0,
        "bar": 1000.0,
        "inchesofmercury": 33.863886666667,
        "inhg": 33.863886666667,
        "millimetersofmercury": 1.3332236842105,
        "mmhg": 1.3332236842105,
        "torr": 1.3332236842105,
        "psi": 68.94757293168,
    },
    "speed": {
        "meterspersecond": 1.0,
        "meterpersecond": 1.0,
        "mps": 1.0,
        "ms": 1.0,
        "m/s": 1.0,
        "kilometersperhour": 1 / 3.6,
        "kph": 1 / 3.6,
        "kmh": 1 / 3.6,
        "km/h": 1 / 3.6,
        "milesperhour": 0.44704,
        "mph": 0.44704,
        "mi/h": 0.44704,
        "knots": 0.514444444444,
        "knot": 0.514444444444,
        "kt": 0.514444444444,
        "kts": 0.514444444444,
        "feetpersecond": 0.3048,
        "fps": 0.3048,
        "ft/s": 0.3048,
    },
    "length": {
        "millimeters": 1.0,
        "millimeter": 1.0,
        "mm": 1.0,
        "centimeters": 10.0,
        "centimeter": 10.0,
        "cm": 10.0,
        "meters": 1000.0,
        "meter": 1000.0,
        "m": 1000.0,
        "inches": 25.4,
        "inch": 25.4,
        "in": 25.4,
        '"': 25.4,
        "mils": 0.0254,
        "mil": 0.0254,
    },
    "precipitationRate": {
        "millimetersperhour": 1.0,
        "mm/h": 1.0,
        "mm/hr": 1.0,
        "mmh": 1.0,
        "centimetersperhour": 10.0,
        "cm/h": 10.0,
        "inchesperhour": 25.4,
        "in/h": 25.4,
        "in/hr": 25.4,
        "inh": 25.4,
        "inhr": 25.4,
        "millimetersperminute": 60.0,
        "mm/min": 60.0,
    },
    "distance": {
        "meters": 1.0,
        "meter": 1.0,
        "m": 1.0,
        "kilometers": 1000.0,
        "kilometer": 1000.0,
        "km": 1000.0,
        "miles": 1609.344,
        "mile": 1609.344,
        "mi": 1609.344,
        "feet": 0.3048,
        "foot": 0.3048,
        "ft": 0.3048,
        "nauticalmiles": 1852.0,
        "nmi": 1852.0,
    },
    "ratio": {
        "percent": 1.0,
        "percentage": 1.0,
        "%": 1.0,
        "pct": 1.0,
        "fraction": 100.0,
        "ratio": 100.0,
        "unitinterval": 100.0,
    },
    "irradiance": {
        "wattspersquaremeter": 1.0,
        "w/m2": 1.0,
        "w/m^2": 1.0,
        "w/m²": 1.0,
        "wm2": 1.0,
        "kilowattspersquaremeter": 1000.0,
        "kw/m2": 1000.0,
        # Ambient Weather and Ecowitt consoles report solar radiation in lux.
        # The firmware's own conversion divides by 126.7; it assumes a daylight
        # spectrum and is an approximation, not a unit identity.
        "lux": 1 / 126.7,
        "lx": 1 / 126.7,
        "footcandles": 10.76391 / 126.7,
        "fc": 10.76391 / 126.7,
    },
    "angle": {
        "degrees": 1.0,
        "degree": 1.0,
        "deg": 1.0,
        "°": 1.0,
        "radians": 180 / math.pi,
        "radian": 180 / math.pi,
        "rad": 180 / math.pi,
    },
    "concentration": {
        "microgramspercubicmeter": 1.0,
        "ug/m3": 1.0,
        "µg/m3": 1.0,
        "µg/m³": 1.0,
        "ugm3": 1.0,
        "milligramspercubicmeter": 1000.0,
        "mg/m3": 1000.0,
        "nanogramspercubicmeter": 0.001,
        "ng/m3": 0.001,
    },
    "dimensionless": {
        "": 1.0,
        "none": 1.0,
        "index": 1.0,
        "dimensionless": 1.0,
        "count": 1.0,
        "number": 1.0,
    },
}

#: Temperature needs an offset, so it gets functions rather than factors.
TEMPERATURE: dict[str, Callable[[float], float]] = {
    "celsius": lambda v: v,
    "celcius": lambda v: v,
    "c": lambda v: v,
    "°c": lambda v: v,
    "degc": lambda v: v,
    "fahrenheit": lambda v: (v - 32) * 5 / 9,
    "f": lambda v: (v - 32) * 5 / 9,
    "°f": lambda v: (v - 32) * 5 / 9,
    "degf": lambda v: (v - 32) * 5 / 9,
    "kelvin": lambda v: v - 273.15,
    "k": lambda v: v - 273.15,
}


def normalize_unit(unit: str) -> str:
    """Fold away the spelling differences between ``m/s``, ``metersPerSecond``, ``MPS``."""
    return unit.strip().lower().replace(" ", "").replace("_", "")


def known_units(dimension: Dimension) -> list[str]:
    """Every unit spelling accepted for a dimension, for error messages."""
    if dimension == "temperature":
        return list(TEMPERATURE)
    return list(FACTORS[dimension])


def is_known_unit(dimension: Dimension, unit: str) -> bool:
    key = normalize_unit(unit)
    if dimension == "temperature":
        return key in TEMPERATURE
    return key in FACTORS[dimension]


def dimensions_for_unit(unit: str) -> list[Dimension]:
    """Which dimensions could a unit spelling belong to."""
    return [dimension for dimension in DIMENSIONS if is_known_unit(dimension, unit)]


def converter_for(dimension: Dimension, unit: str) -> Callable[[float], float]:
    key = normalize_unit(unit)
    if dimension == "temperature":
        convert = TEMPERATURE.get(key)
        if convert is None:
            raise ValueError(_unknown_unit_message(dimension, unit))
        return convert

    factor = FACTORS[dimension].get(key)
    if factor is None:
        raise ValueError(_unknown_unit_message(dimension, unit))
    if factor == 1.0:
        return lambda value: value
    return lambda value: value * factor


def to_si(dimension: Dimension, unit: str, value: float) -> float:
    return converter_for(dimension, unit)(value)


def _unknown_unit_message(dimension: Dimension, unit: str) -> str:
    return (
        f'unknown {dimension} unit "{unit}"; expected one of: {", ".join(known_units(dimension))}'
    )
