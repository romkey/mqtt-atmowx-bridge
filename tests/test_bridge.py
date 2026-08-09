"""End to end: a real MQTT broker on one side, a stubbed PDS on the other.

These drive the actual paho client and the real publish path, so they cover the
wiring that unit tests of the individual pieces cannot.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest

from mqtt_atmowx_bridge.atproto.client import AtpClient
from mqtt_atmowx_bridge.atproto.session import SessionManager
from mqtt_atmowx_bridge.atproto.session_store import MemorySessionStore
from mqtt_atmowx_bridge.bridge.bridge import Bridge
from mqtt_atmowx_bridge.config import parse_config
from mqtt_atmowx_bridge.logging_setup import null_logger
from mqtt_atmowx_bridge.observation.quantity import from_quantity

from .broker import MqttTestBroker
from .helpers import FakePds, json_response, session_response

CREATE = "com.atproto.server.createSession"
PUT = "com.atproto.repo.putRecord"
STATION = "at://did:plc:abc123yourdid/net.atmowx.station/3mremies2t222"


@pytest.fixture
def broker() -> Iterator[MqttTestBroker]:
    with MqttTestBroker() as running:
        yield running


@pytest.fixture
def pds() -> FakePds:
    return (
        FakePds()
        .on(CREATE, json_response(session_response()))
        .on(PUT, json_response({"uri": "at://did:plc:test/net.atmowx.observation/abc"}))
    )


def make_bridge(broker: MqttTestBroker, pds: FakePds, **overrides: Any) -> Bridge:
    document: dict[str, Any] = {
        "station": STATION,
        "atproto": {"identifier": "me.example.com", "password": "abcd-efgh-ijkl-mnop"},
        "mqtt": {"url": broker.url},
        "publish": {"mode": "onMessage", "minIntervalSeconds": 0, "roundToSeconds": 0},
        "sources": [
            {
                "topic": "weather/outdoor",
                "payload": "json",
                "timestamp": {"path": "dateutc", "format": "epochMillis"},
                "trigger": True,
                "map": {
                    "tempf": {"field": "temperature", "unit": "fahrenheit"},
                    "humidity": {"field": "relativeHumidity", "unit": "percent"},
                    "baromabsin": {"field": "pressureStation", "unit": "inHg"},
                    "windspeedmph": {"field": "wind.speed", "unit": "mph"},
                    "windgustmph": {"field": "wind.gust", "unit": "mph"},
                    "winddir": {"field": "wind.direction", "unit": "degrees"},
                    "solarradiation": {"field": "solarIrradiance", "unit": "w/m2"},
                    "uv": {"field": "uvIndex"},
                },
            }
        ],
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(document.get(key), dict):
            document[key] = {**document[key], **value}
        else:
            document[key] = value

    config = parse_config(document)
    session = SessionManager(
        service="https://bsky.social",
        identifier="me.example.com",
        password="abcd-efgh-ijkl-mnop",
        store=MemorySessionStore(),
        client=pds.client(),
    )
    client = AtpClient(session, client=pds.client(), max_retries=0)
    return Bridge(config=config, client=client, logger=null_logger())


def ambient_payload(**overrides: Any) -> str:
    """A payload shaped like an Ambient Weather station's MQTT output."""
    payload: dict[str, Any] = {
        "dateutc": int(time.time() * 1000),
        "tempf": 82.6,
        "humidity": 34,
        "baromabsin": 29.86,
        "windspeedmph": 6.0,
        "windgustmph": 13.0,
        "winddir": 315,
        "solarradiation": 784,
        "uv": 7,
    }
    payload.update(overrides)
    return json.dumps(payload)


def wait_for(condition: Any, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


class TestEndToEnd:
    def test_publishes_an_observation_from_an_mqtt_message(
        self, broker: MqttTestBroker, pds: FakePds
    ) -> None:
        bridge = make_bridge(broker, pds)
        bridge.start()
        try:
            assert bridge.wait_until_connected(10)
            assert broker.wait_for_subscription(5)
            broker.publish("weather/outdoor", ambient_payload())

            assert wait_for(lambda: pds.calls_to(PUT))
        finally:
            bridge.stop()

        record = pds.calls_to(PUT)[0].body["record"]
        assert record["$type"] == "net.atmowx.observation"
        assert record["station"] == STATION
        assert from_quantity(record["temperature"]) == pytest.approx(28.1, abs=0.05)
        assert record["relativeHumidity"] == {"value": 34}
        assert from_quantity(record["pressureStation"]) == pytest.approx(1011.2, abs=0.1)
        assert from_quantity(record["wind"]["speed"]) == pytest.approx(2.7, abs=0.05)
        assert from_quantity(record["wind"]["gust"]) == pytest.approx(5.8, abs=0.05)
        assert record["wind"]["direction"] == 315
        assert record["solarIrradiance"] == {"value": 784}
        assert record["uvIndex"] == {"value": 7}

    def test_publishes_only_the_fields_the_payload_carried(
        self, broker: MqttTestBroker, pds: FakePds
    ) -> None:
        bridge = make_bridge(broker, pds)
        bridge.start()
        try:
            assert bridge.wait_until_connected(10)
            assert broker.wait_for_subscription(5)
            broker.publish(
                "weather/outdoor",
                json.dumps({"dateutc": int(time.time() * 1000), "tempf": 68.0}),
            )

            assert wait_for(lambda: pds.calls_to(PUT))
        finally:
            bridge.stop()

        record = pds.calls_to(PUT)[0].body["record"]
        assert set(record) == {"$type", "station", "observedAt", "temperature"}

    def test_fills_in_a_derived_dew_point(self, broker: MqttTestBroker, pds: FakePds) -> None:
        bridge = make_bridge(broker, pds, derive={"dewPoint": "whenMissing"})
        bridge.start()
        try:
            assert bridge.wait_until_connected(10)
            assert broker.wait_for_subscription(5)
            broker.publish("weather/outdoor", ambient_payload(tempf=68.0, humidity=50))

            assert wait_for(lambda: pds.calls_to(PUT))
        finally:
            bridge.stop()

        record = pds.calls_to(PUT)[0].body["record"]
        # 20 °C at 50 % RH has a dew point near 9.3 °C.
        assert from_quantity(record["dewPoint"]) == pytest.approx(9.3, abs=0.1)

    def test_reduces_station_pressure_to_sea_level(
        self, broker: MqttTestBroker, pds: FakePds
    ) -> None:
        bridge = make_bridge(
            broker,
            pds,
            derive={"pressureSeaLevel": "whenMissing", "elevationMeters": 500},
        )
        bridge.start()
        try:
            assert bridge.wait_until_connected(10)
            assert broker.wait_for_subscription(5)
            broker.publish("weather/outdoor", ambient_payload())

            assert wait_for(lambda: pds.calls_to(PUT))
        finally:
            bridge.stop()

        record = pds.calls_to(PUT)[0].body["record"]
        assert from_quantity(record["pressureSeaLevel"]) > from_quantity(record["pressureStation"])

    def test_carries_forward_readings_from_earlier_messages(
        self, broker: MqttTestBroker, pds: FakePds
    ) -> None:
        bridge = make_bridge(
            broker,
            pds,
            sources=[
                {
                    "topic": "sensors/rain",
                    "field": "precipitation.day",
                    "unit": "inches",
                },
                {
                    "topic": "sensors/temp",
                    "field": "temperature",
                    "unit": "fahrenheit",
                    "trigger": True,
                },
            ],
        )
        bridge.start()
        try:
            assert bridge.wait_until_connected(10)
            assert broker.wait_for_subscription(5)

            # Rain arrives on its own topic and does not trigger a publish.
            broker.publish("sensors/rain", "0.5")
            assert wait_for(lambda: bridge.collector.size == 1)
            broker.publish("sensors/temp", "68.0")

            assert wait_for(lambda: pds.calls_to(PUT))
        finally:
            bridge.stop()

        record = pds.calls_to(PUT)[0].body["record"]
        assert from_quantity(record["precipitation"]["day"]) == pytest.approx(12.7, abs=0.05)
        assert from_quantity(record["temperature"]) == pytest.approx(20.0, abs=0.05)

    def test_uses_the_payload_timestamp_for_observed_at(
        self, broker: MqttTestBroker, pds: FakePds
    ) -> None:
        taken_at = datetime.now(UTC).replace(microsecond=0)
        bridge = make_bridge(broker, pds)
        bridge.start()
        try:
            assert bridge.wait_until_connected(10)
            assert broker.wait_for_subscription(5)
            broker.publish(
                "weather/outdoor",
                ambient_payload(dateutc=int(taken_at.timestamp() * 1000)),
            )

            assert wait_for(lambda: pds.calls_to(PUT))
        finally:
            bridge.stop()

        record = pds.calls_to(PUT)[0].body["record"]
        assert record["observedAt"] == taken_at.isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        )

    def test_does_not_publish_a_stale_reading(self, broker: MqttTestBroker, pds: FakePds) -> None:
        bridge = make_bridge(broker, pds, publish={"maxReadingAgeSeconds": 60})
        bridge.start()
        try:
            assert bridge.wait_until_connected(10)
            assert broker.wait_for_subscription(5)
            # An hour-old reading is not current weather.
            broker.publish(
                "weather/outdoor",
                ambient_payload(dateutc=int((time.time() - 3600) * 1000)),
            )

            assert wait_for(lambda: bridge.collector.size > 0)
            time.sleep(0.3)
        finally:
            bridge.stop()

        assert pds.calls_to(PUT) == []

    def test_survives_a_malformed_payload(self, broker: MqttTestBroker, pds: FakePds) -> None:
        bridge = make_bridge(broker, pds)
        bridge.start()
        try:
            assert bridge.wait_until_connected(10)
            assert broker.wait_for_subscription(5)

            broker.publish("weather/outdoor", "not json at all")
            broker.publish("weather/outdoor", ambient_payload())

            assert wait_for(lambda: pds.calls_to(PUT))
        finally:
            bridge.stop()

        assert len(pds.calls_to(PUT)) == 1

    def test_respects_the_minimum_interval_between_publishes(
        self, broker: MqttTestBroker, pds: FakePds
    ) -> None:
        bridge = make_bridge(broker, pds, publish={"minIntervalSeconds": 3600})
        bridge.start()
        try:
            assert bridge.wait_until_connected(10)
            assert broker.wait_for_subscription(5)

            broker.publish("weather/outdoor", ambient_payload())
            assert wait_for(lambda: bridge.collector.size > 0)
            assert wait_for(lambda: pds.calls_to(PUT))

            broker.publish("weather/outdoor", ambient_payload(tempf=70.0))
            time.sleep(0.3)
        finally:
            bridge.stop()

        assert len(pds.calls_to(PUT)) == 1

    def test_reports_its_state(self, broker: MqttTestBroker, pds: FakePds) -> None:
        bridge = make_bridge(broker, pds)
        bridge.start()
        try:
            assert bridge.wait_until_connected(10)
            assert broker.wait_for_subscription(5)
            broker.publish("weather/outdoor", ambient_payload())
            assert wait_for(lambda: pds.calls_to(PUT))

            status = bridge.status()
        finally:
            bridge.stop()

        assert status["mqtt"]["connected"] is True
        assert status["mqtt"]["messages_received"] >= 1
        assert status["publisher"]["published"] == 1
        assert status["collector"]["readings"] == 8


class TestOnMessageThrottle:
    def test_the_first_publish_is_not_throttled(self, broker: MqttTestBroker, pds: FakePds) -> None:
        bridge = make_bridge(broker, pds, publish={"minIntervalSeconds": 3600})

        assert bridge._last_published_at is None
        # A host whose monotonic clock is still small used to look "recently
        # published" when _last_published_at was 0.0, sleeping for hours.
        assert bridge._seconds_since_last_publish() == float("inf")

    def test_subsequent_publishes_honour_the_minimum_interval(
        self, broker: MqttTestBroker, pds: FakePds
    ) -> None:
        bridge = make_bridge(broker, pds, publish={"minIntervalSeconds": 3600})
        bridge._last_published_at = 1000.0

        with patch("mqtt_atmowx_bridge.bridge.bridge.time.monotonic", return_value=1500.0):
            assert bridge._seconds_since_last_publish() == 500.0

        with patch("mqtt_atmowx_bridge.bridge.bridge.time.monotonic", return_value=5000.0):
            assert bridge._seconds_since_last_publish() == 4000.0


class TestDryRun:
    def test_builds_but_does_not_publish(self, broker: MqttTestBroker, pds: FakePds) -> None:
        bridge = make_bridge(broker, pds, publish={"dryRun": True})
        bridge.start()
        try:
            assert bridge.wait_until_connected(10)
            assert broker.wait_for_subscription(5)
            broker.publish("weather/outdoor", ambient_payload())

            assert wait_for(lambda: bridge.status()["observations_built"] == 1)
            time.sleep(0.2)
        finally:
            bridge.stop()

        assert pds.calls_to(PUT) == []
