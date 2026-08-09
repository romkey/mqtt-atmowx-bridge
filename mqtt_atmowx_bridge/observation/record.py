"""Assembling a net.atmowx.observation record from the readings we happen to have.

Only fields that arrived over MQTT (or were explicitly derived from them) end up
in the record — absent sensors stay absent rather than being published as zero.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import datetime
from typing import Any

from .fields import FIELD_NAMES, ExtraTarget, Target, field_spec
from .quantity import Quantity, to_quantity
from .units import SI_UNIT, Dimension

OBSERVATION_NSID = "net.atmowx.observation"
STATION_NSID = "net.atmowx.station"
MEASUREMENT_TYPE = "net.atmowx.defs#measurement"

MAX_EXTRA = 64


@dataclass(slots=True)
class Reading:
    """A single value, already converted to the SI unit of its target field."""

    target: Target
    #: Value in the SI unit of ``dimension``.
    value: float
    dimension: Dimension
    decimals: int
    #: When the sensor took the reading.
    observed_at: datetime
    #: When the bridge received it.
    received_at: datetime
    #: Where it came from, for logs.
    source: str
    #: For ``extra`` measurements: height relative to ground, in meters.
    height: float | None = None


@dataclass(slots=True)
class BuildResult:
    record: dict[str, Any]
    #: Names of the readings actually published.
    included: list[str] = dataclass_field(default_factory=list)
    #: Readings dropped, with the reason, so the caller can log them.
    rejected: list[dict[str, Any]] = dataclass_field(default_factory=list)


def _set_path(target: dict[str, Any], path: str, value: Any) -> None:
    segments = path.split(".")
    node = target
    for segment in segments[:-1]:
        existing = node.get(segment)
        if not isinstance(existing, dict):
            existing = {}
            node[segment] = existing
        node = existing
    node[segments[-1]] = value


def format_observed_at(moment: datetime) -> str:
    """ISO-8601 in UTC with milliseconds, the shape atmowx publishes."""
    return (
        moment.astimezone(tz=moment.tzinfo)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _encode_core(name: str, value: float) -> tuple[bool, Any]:
    """Returns ``(ok, encoded_or_reason)``."""
    spec = field_spec(name)

    normalized = value
    if spec.wraps:
        normalized = value % 360
    else:
        if spec.minimum is not None and normalized < spec.minimum:
            return False, f"below the plausible minimum of {spec.minimum}"
        if spec.maximum is not None and normalized > spec.maximum:
            return False, f"above the plausible maximum of {spec.maximum}"

    if spec.encoding == "integer":
        # Rounding 359.7 lands on 360, which is outside the lexicon's 0..359 range.
        rounded = round(normalized)
        return True, (rounded % 360 if spec.wraps else rounded)
    return True, to_quantity(normalized, spec.decimals)


def build_observation(
    *, station: str, observed_at: datetime, readings: dict[str, Reading]
) -> BuildResult:
    """Build one record from the readings that are current."""
    record: dict[str, Any] = {
        "$type": OBSERVATION_NSID,
        "station": station,
        "observedAt": format_observed_at(observed_at),
    }
    result = BuildResult(record=record)

    # Walk FIELD_NAMES rather than the reading map so the record's key order
    # follows the lexicon instead of MQTT arrival order.
    for name in FIELD_NAMES:
        reading = readings.get(name)
        if reading is None or not math.isfinite(reading.value):
            continue

        ok, encoded = _encode_core(name, reading.value)
        if not ok:
            result.rejected.append({"name": name, "value": reading.value, "reason": encoded})
            continue
        _set_path(record, field_spec(name).path, encoded)
        result.included.append(name)

    extra: list[dict[str, Any]] = []
    for name, reading in readings.items():
        if not isinstance(reading.target, ExtraTarget) or not math.isfinite(reading.value):
            continue
        if len(extra) >= MAX_EXTRA:
            result.rejected.append(
                {
                    "name": name,
                    "value": reading.value,
                    "reason": f"the extra array is capped at {MAX_EXTRA} measurements",
                }
            )
            continue

        measurement: dict[str, Any] = {
            "$type": MEASUREMENT_TYPE,
            "parameter": reading.target.parameter,
            "value": to_quantity(reading.value, reading.decimals),
            "unit": SI_UNIT[reading.dimension],
        }
        if reading.height is not None:
            measurement["height"] = to_quantity(reading.height, 2)
        extra.append(measurement)
        result.included.append(name)

    if extra:
        extra.sort(key=lambda item: str(item["parameter"]))
        record["extra"] = extra

    return result


__all__ = [
    "MAX_EXTRA",
    "MEASUREMENT_TYPE",
    "OBSERVATION_NSID",
    "STATION_NSID",
    "BuildResult",
    "Quantity",
    "Reading",
    "build_observation",
    "format_observed_at",
]
