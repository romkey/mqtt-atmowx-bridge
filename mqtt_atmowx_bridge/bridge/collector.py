"""Turning MQTT messages into readings.

The collector holds the most recent value for every mapped measurement. Sensors
publish on their own schedules — temperature every 30 seconds, rain only when it
rains — so an observation is assembled from whatever is current at publish time
rather than from a single message.

Messages arrive on paho's network thread while the publisher reads on its own,
so the reading map is guarded by a lock.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..config import Config, ResolvedJsonSource, ResolvedMapping, ResolvedScalarSource
from ..logging_setup import BridgeLogger
from ..mqtt.client import IncomingMessage
from ..mqtt.topic import topic_matches
from ..observation.record import Reading
from ..observation.units import to_si

_NUMBER_PATTERN = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")
_INDEX_PATTERN = re.compile(r"\[(\d+)\]")


@dataclass(frozen=True, slots=True)
class IngestResult:
    #: How many readings this message updated.
    updated: int
    #: Whether a ``trigger: true`` source produced at least one reading.
    triggered: bool


class ReadingCollector:
    """The current value of every mapped measurement."""

    def __init__(self, config: Config, logger: BridgeLogger) -> None:
        self._config = config
        self._log = logger
        self._readings: dict[str, Reading] = {}
        self._lock = threading.Lock()
        self._warned: set[str] = set()
        self._ignored_retained = 0
        self._unmatched_messages = 0

    def ingest(self, message: IncomingMessage) -> IngestResult:
        updated = 0
        triggered = False
        matched_any_source = False

        for source in self._config.sources:
            if not topic_matches(source.topic, message.topic):
                continue
            matched_any_source = True

            if message.retain and source.ignore_retained:
                self._ignored_retained += 1
                self._log.debug("ignoring a retained message", topic=message.topic)
                continue

            if isinstance(source, ResolvedJsonSource):
                count = self._ingest_json(source, message)
            else:
                count = self._ingest_scalar(source, message)

            updated += count
            if count and source.trigger:
                triggered = True

        if not matched_any_source:
            self._unmatched_messages += 1
            self._warn_once(
                f"unmatched:{message.topic}",
                "received a message on a topic no source maps",
                topic=message.topic,
            )

        return IngestResult(updated=updated, triggered=triggered)

    def snapshot(self, now: datetime, max_age_seconds: float) -> dict[str, Reading]:
        """Readings no older than ``max_age_seconds``, keyed by mapping name."""
        with self._lock:
            items = list(self._readings.items())
        return {
            name: reading
            for name, reading in items
            if (now - reading.observed_at).total_seconds() <= max_age_seconds
        }

    def newest_observed_at(self) -> datetime | None:
        """The newest reading time in the collector, for stamping an observation."""
        with self._lock:
            times = [reading.observed_at for reading in self._readings.values()]
        return max(times) if times else None

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._readings)

    def status(self) -> dict[str, Any]:
        with self._lock:
            names = sorted(self._readings)
        return {
            "readings": len(names),
            "fields": names,
            "ignored_retained": self._ignored_retained,
            "unmatched_messages": self._unmatched_messages,
        }

    # -- decoding -----------------------------------------------------------

    def _ingest_json(self, source: ResolvedJsonSource, message: IncomingMessage) -> int:
        try:
            document = json.loads(message.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._warn_once(
                f"json:{message.topic}",
                "payload is not valid JSON",
                topic=message.topic,
                payload=_truncate(message.payload),
            )
            return 0

        root = get_path(document, source.root) if source.root else document
        if not isinstance(root, dict):
            self._warn_once(
                f"root:{message.topic}",
                "payload does not contain the configured root object",
                topic=message.topic,
                root=source.root,
            )
            return 0

        observed_at = self._observed_at_for(source, root, message)

        updated = 0
        for path, mapping in source.mappings:
            raw = get_path(root, path)
            if raw is None:
                continue

            value = coerce_number(raw)
            if value is None:
                self._warn_once(
                    f"value:{message.topic}:{path}",
                    "value is not a number",
                    topic=message.topic,
                    path=path,
                    value=_truncate(str(raw)),
                )
                continue

            if self._record(mapping, value, observed_at, message, f"{message.topic}#{path}"):
                updated += 1
        return updated

    def _ingest_scalar(self, source: ResolvedScalarSource, message: IncomingMessage) -> int:
        try:
            text = message.payload.decode("utf-8").strip()
        except UnicodeDecodeError:
            self._warn_once(
                f"scalar:{message.topic}",
                "payload is not UTF-8 text",
                topic=message.topic,
            )
            return 0
        if not text:
            return 0

        raw = coerce_number(text) if source.payload == "number" else extract_number(text)
        if raw is None:
            self._warn_once(
                f"scalar:{message.topic}",
                "payload does not contain a number",
                topic=message.topic,
                payload=_truncate(text),
            )
            return 0

        # A bare value topic carries no timestamp, so arrival time is all we have.
        return (
            1
            if self._record(source.mapping, raw, message.received_at, message, message.topic)
            else 0
        )

    def _observed_at_for(
        self, source: ResolvedJsonSource, root: dict[str, Any], message: IncomingMessage
    ) -> datetime:
        if self._config.publish.timestamp_source == "received" or source.timestamp is None:
            return message.received_at

        raw = get_path(root, source.timestamp.path)
        parsed = parse_timestamp(raw, source.timestamp.format)
        if parsed is None:
            self._warn_once(
                f"timestamp:{message.topic}",
                "could not read the payload timestamp; using the arrival time instead",
                topic=message.topic,
                path=source.timestamp.path,
                value=_truncate(str(raw)),
            )
            return message.received_at
        return parsed

    def _record(
        self,
        mapping: ResolvedMapping,
        raw_value: float,
        observed_at: datetime,
        message: IncomingMessage,
        source_label: str,
    ) -> bool:
        """Calibrate, range-check, convert to SI, and store."""
        calibrated = raw_value * mapping.multiplier + mapping.offset

        if mapping.ignore_below is not None and calibrated < mapping.ignore_below:
            return False
        if mapping.ignore_above is not None and calibrated > mapping.ignore_above:
            return False

        try:
            value = to_si(mapping.dimension, mapping.unit, calibrated)
        except (ValueError, OverflowError):
            return False
        if value != value or value in (float("inf"), float("-inf")):
            return False

        reading = Reading(
            target=mapping.target,
            value=value,
            dimension=mapping.dimension,
            decimals=mapping.decimals,
            observed_at=observed_at,
            received_at=message.received_at,
            source=source_label,
            height=mapping.height,
        )
        with self._lock:
            self._readings[mapping.name] = reading

        self._log.debug(
            "reading updated",
            field=mapping.name,
            raw=raw_value,
            unit=mapping.unit,
            si=value,
            source=source_label,
        )
        return True

    def _warn_once(self, key: str, message: str, **fields: Any) -> None:
        """Warn the first time, then drop to debug — one bad publisher should
        not drown the log."""
        if key in self._warned:
            self._log.debug(message, **fields)
            return
        self._warned.add(key)
        self._log.warn(message, **fields)


def get_path(document: Any, path: str) -> Any:
    """Read ``a.b[0].c`` out of a parsed JSON document."""
    segments = [segment for segment in _INDEX_PATTERN.sub(r".\1", path).split(".") if segment != ""]

    node = document
    for segment in segments:
        if node is None:
            return None
        if isinstance(node, list):
            try:
                index = int(segment)
            except ValueError:
                return None
            if not -len(node) <= index < len(node):
                return None
            node = node[index]
            continue
        if not isinstance(node, dict):
            return None
        node = node.get(segment)
    return node


def coerce_number(value: Any) -> float | None:
    """Numbers arrive as numbers or as strings, depending on the publisher."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value) if value == value and abs(value) != float("inf") else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = float(text)
        except ValueError:
            return None
        return parsed if parsed == parsed and abs(parsed) != float("inf") else None
    return None


def extract_number(text: str) -> float | None:
    """Pull the first number out of a string like ``21.4 °C`` or ``wind 3.2 m/s``."""
    match = _NUMBER_PATTERN.search(text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def parse_timestamp(raw: Any, fmt: str) -> datetime | None:
    if raw is None:
        return None

    if fmt == "iso":
        if not isinstance(raw, str):
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    numeric = coerce_number(raw)
    if numeric is None:
        return None
    seconds = numeric if fmt == "epochSeconds" else numeric / 1000.0
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _truncate(text: str | bytes, limit: int = 120) -> str:
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    return text if len(text) <= limit else f"{text[:limit]}…"
