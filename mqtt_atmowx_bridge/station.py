"""net.atmowx.station records — the thing an observation's ``station`` points at.

A station is created once and then referenced by every observation, so this is a
setup-time helper rather than part of the publish path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .atproto.client import AtpClient
from .atproto.tid import now_tid
from .observation.quantity import to_quantity
from .observation.record import STATION_NSID

KINDS = ("personal", "amateur", "professional", "official", "research")
STATUSES = ("active", "inactive", "maintenance", "retired")


@dataclass(slots=True)
class StationInput:
    name: str
    latitude: float
    longitude: float
    elevation_meters: float | None = None
    description: str | None = None
    kind: str | None = None
    status: str | None = None
    hardware: str | None = None
    software: str | None = None
    timezone: str | None = None


def build_station_record(station: StationInput) -> dict[str, Any]:
    if not -90 <= station.latitude <= 90:
        raise ValueError(f"latitude must be between -90 and 90, got {station.latitude}")
    if not -180 <= station.longitude <= 180:
        raise ValueError(f"longitude must be between -180 and 180, got {station.longitude}")
    if station.kind is not None and station.kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    if station.status is not None and station.status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")

    record: dict[str, Any] = {
        "$type": STATION_NSID,
        "name": station.name,
        "location": {
            # 5 decimal places is about a meter — plenty for a fixed station,
            # and less precise than a GPS trace of where someone lives.
            "latitude": to_quantity(station.latitude, 5),
            "longitude": to_quantity(station.longitude, 5),
        },
        "createdAt": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    }

    if station.elevation_meters is not None:
        record["location"]["elevation"] = to_quantity(station.elevation_meters, 1)
    for key, value in (
        ("description", station.description),
        ("kind", station.kind),
        ("status", station.status),
        ("hardware", station.hardware),
        ("software", station.software),
        ("timezone", station.timezone),
    ):
        if value is not None:
            record[key] = value

    return record


def create_station(client: AtpClient, station: StationInput) -> dict[str, Any]:
    """Create the station record and return its ``uri``/``cid``."""
    record = build_station_record(station)
    return client.create_record(collection=STATION_NSID, record=record, rkey=now_tid())


def list_stations(client: AtpClient, limit: int = 50) -> list[dict[str, Any]]:
    response = client.list_records(collection=STATION_NSID, limit=limit)
    records = response.get("records")
    return records if isinstance(records, list) else []
