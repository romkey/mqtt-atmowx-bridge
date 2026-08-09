"""Ingesting MQTT payloads into readings."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from mqtt_atmowx_bridge.bridge.collector import (
    ReadingCollector,
    coerce_number,
    extract_number,
    get_path,
    parse_timestamp,
)
from mqtt_atmowx_bridge.config import parse_config
from mqtt_atmowx_bridge.logging_setup import null_logger
from mqtt_atmowx_bridge.mqtt.client import IncomingMessage
from mqtt_atmowx_bridge.mqtt.topic import topic_matches

STATION = "at://did:plc:abc123yourdid/net.atmowx.station/3mremies2t222"
NOW = datetime(2026, 8, 7, 21, 35, 0, tzinfo=UTC)
NOW_EPOCH = int(NOW.timestamp())


def config_with(sources: list[dict[str, Any]], **publish: Any):  # type: ignore[no-untyped-def]
    document: dict[str, Any] = {
        "station": STATION,
        "atproto": {"identifier": "me.example.com", "password": "abcd-efgh-ijkl-mnop"},
        "mqtt": {"url": "mqtt://broker.local"},
        "sources": sources,
    }
    if publish:
        document["publish"] = publish
    return parse_config(document)


def collector_for(sources: list[dict[str, Any]], **publish: Any) -> ReadingCollector:
    return ReadingCollector(config_with(sources, **publish), null_logger())


def message(
    topic: str, payload: Any, retain: bool = False, received_at: datetime = NOW
) -> IncomingMessage:
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    return IncomingMessage(
        topic=topic, payload=raw.encode("utf-8"), retain=retain, received_at=received_at
    )


class TestJsonPayloads:
    def test_reads_mapped_fields_and_converts_them(self) -> None:
        collector = collector_for(
            [
                {
                    "topic": "weather/outdoor",
                    "payload": "json",
                    "map": {
                        "tempf": {"field": "temperature", "unit": "fahrenheit"},
                        "humidity": {"field": "relativeHumidity", "unit": "percent"},
                    },
                }
            ]
        )

        result = collector.ingest(message("weather/outdoor", {"tempf": 68.0, "humidity": 55}))

        assert result.updated == 2
        readings = collector.snapshot(NOW, 900)
        assert readings["temperature"].value == pytest.approx(20.0)
        assert readings["relativeHumidity"].value == 55

    def test_ignores_fields_the_payload_does_not_carry(self) -> None:
        collector = collector_for(
            [
                {
                    "topic": "weather/outdoor",
                    "payload": "json",
                    "map": {
                        "tempf": {"field": "temperature", "unit": "fahrenheit"},
                        "uv": {"field": "uvIndex"},
                    },
                }
            ]
        )

        collector.ingest(message("weather/outdoor", {"tempf": 68.0}))

        assert set(collector.snapshot(NOW, 900)) == {"temperature"}

    def test_reads_from_a_nested_root(self) -> None:
        collector = collector_for(
            [
                {
                    "topic": "shadow/update",
                    "payload": "json",
                    "root": "state.reported",
                    "map": {"temp": {"field": "temperature", "unit": "celsius"}},
                }
            ]
        )

        collector.ingest(message("shadow/update", {"state": {"reported": {"temp": 21.5}}}))

        assert collector.snapshot(NOW, 900)["temperature"].value == 21.5

    def test_reads_a_dotted_and_indexed_path(self) -> None:
        collector = collector_for(
            [
                {
                    "topic": "sensors",
                    "payload": "json",
                    "map": {"probes[1].value": {"field": "temperature", "unit": "celsius"}},
                }
            ]
        )

        collector.ingest(message("sensors", {"probes": [{"value": 10.0}, {"value": 21.5}]}))

        assert collector.snapshot(NOW, 900)["temperature"].value == 21.5

    def test_accepts_a_number_sent_as_a_string(self) -> None:
        collector = collector_for(
            [
                {
                    "topic": "weather",
                    "payload": "json",
                    "map": {"t": {"field": "temperature", "unit": "celsius"}},
                }
            ]
        )

        collector.ingest(message("weather", {"t": "21.5"}))

        assert collector.snapshot(NOW, 900)["temperature"].value == 21.5

    def test_survives_a_payload_that_is_not_json(self) -> None:
        collector = collector_for(
            [
                {
                    "topic": "weather",
                    "payload": "json",
                    "map": {"t": {"field": "temperature", "unit": "celsius"}},
                }
            ]
        )

        assert collector.ingest(message("weather", "<html>oops</html>")).updated == 0
        assert collector.snapshot(NOW, 900) == {}


class TestTimestamps:
    def test_uses_the_payload_timestamp(self) -> None:
        collector = collector_for(
            [
                {
                    "topic": "weather",
                    "payload": "json",
                    "timestamp": {"path": "dateutc", "format": "epochMillis"},
                    "map": {"t": {"field": "temperature", "unit": "celsius"}},
                }
            ]
        )

        collector.ingest(
            message("weather", {"t": 21.5, "dateutc": NOW_EPOCH * 1000}, received_at=NOW)
        )

        assert collector.snapshot(NOW, 900)["temperature"].observed_at == NOW

    def test_parses_an_iso_timestamp(self) -> None:
        collector = collector_for(
            [
                {
                    "topic": "weather",
                    "payload": "json",
                    "timestamp": {"path": "time", "format": "iso"},
                    "map": {"t": {"field": "temperature", "unit": "celsius"}},
                }
            ]
        )

        collector.ingest(message("weather", {"t": 21.5, "time": "2026-08-07T21:35:00Z"}))

        assert collector.snapshot(NOW, 900)["temperature"].observed_at == NOW

    def test_falls_back_to_the_arrival_time_when_the_timestamp_is_unusable(self) -> None:
        collector = collector_for(
            [
                {
                    "topic": "weather",
                    "payload": "json",
                    "timestamp": {"path": "time", "format": "iso"},
                    "map": {"t": {"field": "temperature", "unit": "celsius"}},
                }
            ]
        )

        collector.ingest(message("weather", {"t": 21.5, "time": "not a date"}, received_at=NOW))

        assert collector.snapshot(NOW, 900)["temperature"].observed_at == NOW

    def test_ignores_the_payload_timestamp_when_configured_to(self) -> None:
        collector = collector_for(
            [
                {
                    "topic": "weather",
                    "payload": "json",
                    "timestamp": {"path": "time", "format": "iso"},
                    "map": {"t": {"field": "temperature", "unit": "celsius"}},
                }
            ],
            timestampSource="received",
        )

        collector.ingest(
            message("weather", {"t": 21.5, "time": "2020-01-01T00:00:00Z"}, received_at=NOW)
        )

        assert collector.snapshot(NOW, 900)["temperature"].observed_at == NOW


class TestScalarPayloads:
    def test_reads_a_bare_number(self) -> None:
        collector = collector_for(
            [{"topic": "sensor/temp", "field": "temperature", "unit": "celsius"}]
        )

        collector.ingest(message("sensor/temp", "21.5"))

        assert collector.snapshot(NOW, 900)["temperature"].value == 21.5

    def test_pulls_a_number_out_of_a_text_payload(self) -> None:
        collector = collector_for(
            [
                {
                    "topic": "sensor/temp",
                    "payload": "text",
                    "field": "temperature",
                    "unit": "celsius",
                }
            ]
        )

        collector.ingest(message("sensor/temp", "21.5 °C"))

        assert collector.snapshot(NOW, 900)["temperature"].value == 21.5

    def test_stamps_a_scalar_with_its_arrival_time(self) -> None:
        collector = collector_for(
            [{"topic": "sensor/temp", "field": "temperature", "unit": "celsius"}]
        )
        arrived = NOW - timedelta(seconds=30)

        collector.ingest(message("sensor/temp", "21.5", received_at=arrived))

        assert collector.snapshot(NOW, 900)["temperature"].observed_at == arrived

    def test_ignores_a_payload_with_no_number_in_it(self) -> None:
        collector = collector_for(
            [{"topic": "sensor/temp", "field": "temperature", "unit": "celsius"}]
        )

        assert collector.ingest(message("sensor/temp", "unavailable")).updated == 0

    def test_matches_a_wildcard_subscription(self) -> None:
        collector = collector_for(
            [{"topic": "sensors/+/temperature", "field": "temperature", "unit": "celsius"}]
        )

        assert collector.ingest(message("sensors/garden/temperature", "21.5")).updated == 1


class TestCalibrationAndFiltering:
    def test_applies_the_multiplier_and_offset_before_converting(self) -> None:
        collector = collector_for(
            [
                {
                    "topic": "sensor/temp",
                    "field": "temperature",
                    "unit": "celsius",
                    "multiplier": 0.1,
                    "offset": -0.5,
                }
            ]
        )

        collector.ingest(message("sensor/temp", "215"))

        assert collector.snapshot(NOW, 900)["temperature"].value == pytest.approx(21.0)

    def test_drops_a_reading_below_the_ignore_threshold(self) -> None:
        collector = collector_for(
            [
                {
                    "topic": "sensor/temp",
                    "field": "temperature",
                    "unit": "celsius",
                    "ignoreBelow": -40,
                }
            ]
        )

        # -100 is what a disconnected probe reports, not weather.
        assert collector.ingest(message("sensor/temp", "-100")).updated == 0
        assert collector.ingest(message("sensor/temp", "-20")).updated == 1

    def test_drops_a_reading_above_the_ignore_threshold(self) -> None:
        collector = collector_for(
            [
                {
                    "topic": "sensor/hum",
                    "field": "relativeHumidity",
                    "unit": "percent",
                    "ignoreAbove": 100,
                }
            ]
        )

        assert collector.ingest(message("sensor/hum", "255")).updated == 0

    def test_ignores_retained_messages_when_asked(self) -> None:
        collector = collector_for(
            [
                {
                    "topic": "sensor/temp",
                    "field": "temperature",
                    "unit": "celsius",
                    "ignoreRetained": True,
                }
            ]
        )

        # A retained message is whatever was last published, possibly days ago.
        assert collector.ingest(message("sensor/temp", "21.5", retain=True)).updated == 0
        assert collector.ingest(message("sensor/temp", "21.5")).updated == 1


class TestStaleness:
    def test_omits_readings_older_than_the_cutoff(self) -> None:
        collector = collector_for(
            [
                {"topic": "sensor/temp", "field": "temperature", "unit": "celsius"},
                {"topic": "sensor/uv", "field": "uvIndex"},
            ]
        )

        collector.ingest(message("sensor/temp", "21.5", received_at=NOW - timedelta(hours=2)))
        collector.ingest(message("sensor/uv", "4", received_at=NOW))

        assert set(collector.snapshot(NOW, 900)) == {"uvIndex"}

    def test_keeps_the_most_recent_value_for_a_field(self) -> None:
        collector = collector_for(
            [{"topic": "sensor/temp", "field": "temperature", "unit": "celsius"}]
        )

        collector.ingest(message("sensor/temp", "21.5"))
        collector.ingest(message("sensor/temp", "22.0"))

        assert collector.snapshot(NOW, 900)["temperature"].value == 22.0
        assert collector.size == 1


class TestTriggers:
    def test_reports_a_trigger_source(self) -> None:
        collector = collector_for(
            [
                {
                    "topic": "weather/complete",
                    "field": "temperature",
                    "unit": "celsius",
                    "trigger": True,
                },
                {"topic": "sensor/uv", "field": "uvIndex"},
            ],
            mode="onMessage",
        )

        assert collector.ingest(message("weather/complete", "21.5")).triggered is True
        assert collector.ingest(message("sensor/uv", "4")).triggered is False

    def test_does_not_trigger_when_nothing_was_read(self) -> None:
        collector = collector_for(
            [
                {
                    "topic": "weather/complete",
                    "field": "temperature",
                    "unit": "celsius",
                    "trigger": True,
                }
            ],
            mode="onMessage",
        )

        assert collector.ingest(message("weather/complete", "n/a")).triggered is False


class TestPathAndNumberHelpers:
    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("a.b", 1),
            ("a.c[0]", 2),
            ("a.c[1].d", 3),
            ("a.missing", None),
            ("a.c[9]", None),
        ],
    )
    def test_reads_a_json_path(self, path: str, expected: Any) -> None:
        document = {"a": {"b": 1, "c": [2, {"d": 3}]}}
        assert get_path(document, path) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [(5, 5.0), ("5.5", 5.5), (" 5 ", 5.0), (True, None), ("abc", None), (None, None)],
    )
    def test_coerces_a_number(self, value: Any, expected: float | None) -> None:
        assert coerce_number(value) == expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [("21.5 °C", 21.5), ("wind 3.2 m/s", 3.2), ("-4", -4.0), ("1e3", 1000.0), ("none", None)],
    )
    def test_extracts_a_number_from_text(self, text: str, expected: float | None) -> None:
        assert extract_number(text) == expected

    def test_parses_the_supported_timestamp_formats(self) -> None:
        assert parse_timestamp("2026-08-07T21:35:00Z", "iso") == NOW
        assert parse_timestamp(NOW_EPOCH, "epochSeconds") == NOW
        assert parse_timestamp(NOW_EPOCH * 1000, "epochMillis") == NOW
        assert parse_timestamp("nope", "iso") is None


class TestTopicMatching:
    @pytest.mark.parametrize(
        ("filter_", "topic", "expected"),
        [
            ("a/b", "a/b", True),
            ("a/+", "a/b", True),
            ("a/+", "a/b/c", False),
            ("a/#", "a/b/c", True),
            ("a/#", "a", True),
            ("#", "a/b", True),
            ("+/b", "a/b", True),
            ("a/b", "a/c", False),
            ("#", "$SYS/broker", False),
            ("+/broker", "$SYS/broker", False),
        ],
    )
    def test_matches_per_the_mqtt_spec(self, filter_: str, topic: str, expected: bool) -> None:
        assert topic_matches(filter_, topic) is expected
