"""Config validation, source resolution and environment interpolation."""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest

from mqtt_atmowx_bridge.config import (
    ConfigError,
    ResolvedJsonSource,
    ResolvedScalarSource,
    interpolate_document,
    load_atproto_settings,
    load_config,
    parse_config,
)

STATION = "at://did:plc:abc123yourdid/net.atmowx.station/3mremies2t222"


def minimal(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "station": STATION,
        "atproto": {"identifier": "station.example.com", "password": "abcd-efgh-ijkl-mnop"},
        "mqtt": {"url": "mqtt://broker.local:1883"},
        "sources": [
            {
                "topic": "weather/outdoor",
                "payload": "json",
                "map": {"tempf": {"field": "temperature", "unit": "fahrenheit"}},
            }
        ],
    }
    document.update(overrides)
    return document


class TestDefaults:
    def test_accepts_a_minimal_config(self) -> None:
        config = parse_config(minimal())

        assert config.station == STATION
        assert config.publish.mode == "interval"
        assert config.publish.interval_seconds == 300
        assert config.atproto.service == "https://bsky.social"
        assert config.log.level == "info"

    def test_collects_the_topics_to_subscribe_to(self) -> None:
        config = parse_config(
            minimal(
                sources=[
                    {"topic": "a/b", "field": "temperature", "unit": "celsius"},
                    {"topic": "a/c", "field": "relativeHumidity", "unit": "percent"},
                    {"topic": "a/b", "field": "uvIndex"},
                ]
            )
        )

        # Duplicates collapse into one subscription.
        assert config.topics == ("a/b", "a/c")

    def test_treats_a_source_without_a_payload_type_as_a_bare_number(self) -> None:
        config = parse_config(
            minimal(sources=[{"topic": "sensor/temp", "field": "temperature", "unit": "celsius"}])
        )

        source = config.sources[0]
        assert isinstance(source, ResolvedScalarSource)
        assert source.payload == "number"


class TestValidation:
    def test_rejects_a_station_that_is_not_an_at_uri(self) -> None:
        with pytest.raises(ConfigError, match="AT-URI"):
            parse_config(minimal(station="my-weather-station"))

    def test_rejects_an_unknown_field(self) -> None:
        with pytest.raises(ConfigError, match="unknown observation field"):
            parse_config(
                minimal(sources=[{"topic": "a/b", "field": "temprature", "unit": "celsius"}])
            )

    def test_rejects_a_unit_from_the_wrong_dimension(self) -> None:
        with pytest.raises(ConfigError, match="not a speed unit"):
            parse_config(minimal(sources=[{"topic": "a/b", "field": "wind.speed", "unit": "inHg"}]))

    def test_requires_a_unit_for_a_dimensional_field(self) -> None:
        with pytest.raises(ConfigError, match='needs a "unit"'):
            parse_config(minimal(sources=[{"topic": "a/b", "field": "temperature"}]))

    def test_allows_a_bare_index_field_without_a_unit(self) -> None:
        config = parse_config(minimal(sources=[{"topic": "a/b", "field": "uvIndex"}]))

        assert isinstance(config.sources[0], ResolvedScalarSource)
        assert config.sources[0].mapping.dimension == "dimensionless"

    def test_rejects_an_unknown_key(self) -> None:
        with pytest.raises(ConfigError, match=r"Extra inputs|extra_forbidden|not permitted"):
            parse_config(minimal(publish={"intervalSecond": 60}))

    def test_rejects_a_broker_url_that_is_not_mqtt(self) -> None:
        with pytest.raises(ConfigError, match="mqtt://"):
            parse_config(minimal(mqtt={"url": "https://broker.local"}))

    def test_rejects_a_hash_wildcard_that_is_not_last(self) -> None:
        with pytest.raises(ConfigError, match="last topic level"):
            parse_config(minimal(sources=[{"topic": "a/#/c", "field": "uvIndex"}]))

    def test_rejects_an_empty_map(self) -> None:
        with pytest.raises(ConfigError, match="never produce a reading"):
            parse_config(minimal(sources=[{"topic": "a/b", "payload": "json", "map": {}}]))

    def test_rejects_on_message_mode_with_no_trigger(self) -> None:
        with pytest.raises(ConfigError, match="trigger"):
            parse_config(minimal(publish={"mode": "onMessage"}))

    def test_accepts_on_message_mode_with_a_trigger(self) -> None:
        config = parse_config(
            minimal(
                publish={"mode": "onMessage"},
                sources=[
                    {"topic": "a/b", "field": "temperature", "unit": "celsius", "trigger": True}
                ],
            )
        )
        assert config.publish.mode == "onMessage"


class TestRecordKeyCollisions:
    def test_rejects_rounding_coarser_than_the_publish_interval(self) -> None:
        # Two records 60s apart both round to the same 300s bucket, so the
        # second would overwrite the first.
        with pytest.raises(ConfigError, match="overwrite each other"):
            parse_config(minimal(publish={"intervalSeconds": 60, "roundToSeconds": 300}))

    def test_allows_rounding_equal_to_the_interval(self) -> None:
        config = parse_config(minimal(publish={"intervalSeconds": 300, "roundToSeconds": 300}))
        assert config.publish.round_to_seconds == 300

    def test_checks_the_floor_interval_in_on_message_mode(self) -> None:
        with pytest.raises(ConfigError, match="minIntervalSeconds"):
            parse_config(
                minimal(
                    publish={
                        "mode": "onMessage",
                        "minIntervalSeconds": 30,
                        "roundToSeconds": 600,
                    },
                    sources=[
                        {
                            "topic": "a/b",
                            "field": "temperature",
                            "unit": "celsius",
                            "trigger": True,
                        }
                    ],
                )
            )


class TestDerivedValues:
    def test_requires_the_inputs_a_derivation_needs(self) -> None:
        with pytest.raises(ConfigError, match="relativeHumidity"):
            parse_config(minimal(derive={"dewPoint": "whenMissing"}))

    def test_accepts_a_derivation_whose_inputs_are_mapped(self) -> None:
        config = parse_config(
            minimal(
                derive={"dewPoint": "whenMissing"},
                sources=[
                    {
                        "topic": "a/b",
                        "payload": "json",
                        "map": {
                            "t": {"field": "temperature", "unit": "celsius"},
                            "h": {"field": "relativeHumidity", "unit": "percent"},
                        },
                    }
                ],
            )
        )
        assert config.derive.dew_point == "whenMissing"

    def test_requires_an_elevation_to_reduce_pressure(self) -> None:
        with pytest.raises(ConfigError, match="elevationMeters"):
            parse_config(minimal(derive={"pressureSeaLevel": "whenMissing"}))

    def test_refuses_to_derive_both_pressures_from_each_other(self) -> None:
        with pytest.raises(ConfigError, match="cannot both be enabled"):
            parse_config(
                minimal(
                    derive={
                        "pressureSeaLevel": "always",
                        "pressureStation": "always",
                        "elevationMeters": 100,
                    }
                )
            )


class TestExtraMeasurements:
    def test_infers_the_dimension_from_the_unit(self) -> None:
        config = parse_config(
            minimal(
                sources=[{"topic": "a/b", "field": "extra:leafTemperature", "unit": "fahrenheit"}]
            )
        )

        assert isinstance(config.sources[0], ResolvedScalarSource)
        assert config.sources[0].mapping.dimension == "temperature"

    def test_defaults_to_dimensionless_without_a_unit(self) -> None:
        config = parse_config(minimal(sources=[{"topic": "a/b", "field": "extra:leafWetness"}]))

        assert isinstance(config.sources[0], ResolvedScalarSource)
        assert config.sources[0].mapping.dimension == "dimensionless"

    def test_refuses_an_ambiguous_unit(self) -> None:
        # `m` is both a length and a distance, and they publish differently.
        with pytest.raises(ConfigError, match="ambiguous"):
            parse_config(minimal(sources=[{"topic": "a/b", "field": "extra:height", "unit": "m"}]))


class TestResolvedMappings:
    def test_carries_the_calibration_through(self) -> None:
        config = parse_config(
            minimal(
                sources=[
                    {
                        "topic": "a/b",
                        "field": "temperature",
                        "unit": "celsius",
                        "multiplier": 0.1,
                        "offset": -0.5,
                        "ignoreBelow": -50,
                        "decimals": 2,
                    }
                ]
            )
        )

        mapping = config.sources[0].mapping  # type: ignore[union-attr]
        assert (mapping.multiplier, mapping.offset, mapping.ignore_below) == (0.1, -0.5, -50)
        assert mapping.decimals == 2

    def test_defaults_decimals_to_the_field_precision(self) -> None:
        config = parse_config(
            minimal(
                sources=[
                    {"topic": "a/b", "field": "temperature", "unit": "celsius"},
                    {"topic": "a/c", "field": "relativeHumidity", "unit": "percent"},
                ]
            )
        )

        assert config.sources[0].mapping.decimals == 1  # type: ignore[union-attr]
        assert config.sources[1].mapping.decimals == 0  # type: ignore[union-attr]

    def test_keeps_the_json_paths_of_a_json_source(self) -> None:
        config = parse_config(
            minimal(
                sources=[
                    {
                        "topic": "a/b",
                        "payload": "json",
                        "root": "state.reported",
                        "timestamp": {"path": "ts", "format": "epochSeconds"},
                        "map": {"sensors.tempf": {"field": "temperature", "unit": "fahrenheit"}},
                    }
                ]
            )
        )

        source = config.sources[0]
        assert isinstance(source, ResolvedJsonSource)
        assert source.root == "state.reported"
        assert source.timestamp is not None
        assert source.timestamp.format == "epochSeconds"
        assert source.mappings[0][0] == "sensors.tempf"


class TestEnvironmentInterpolation:
    def test_substitutes_a_variable(self) -> None:
        assert interpolate_document({"a": "${TOKEN}"}, {"TOKEN": "secret"}) == {"a": "secret"}

    def test_uses_a_fallback_when_unset(self) -> None:
        assert interpolate_document("${MISSING:-default}", {}) == "default"

    def test_uses_an_empty_fallback_when_unset(self) -> None:
        assert interpolate_document("${MISSING:-}", {}) == ""

    def test_fails_loudly_on_an_unset_variable_with_no_fallback(self) -> None:
        with pytest.raises(ConfigError, match="MQTT_PASSWORD"):
            interpolate_document("${MQTT_PASSWORD}", {})

    def test_leaves_non_strings_alone(self) -> None:
        assert interpolate_document({"n": 5, "b": True, "x": None}, {}) == {
            "n": 5,
            "b": True,
            "x": None,
        }

    def test_treats_an_empty_credential_as_unset(self) -> None:
        config = parse_config(
            minimal(mqtt={"url": "mqtt://broker.local", "username": "", "password": ""})
        )

        # An anonymous broker must not be handed a username of "".
        assert config.mqtt.username is None
        assert config.mqtt.password is None


class TestLoadFromFile:
    def _write(self, tmp_path: Path, body: str) -> Path:
        path = tmp_path / "config.yaml"
        path.write_text(textwrap.dedent(body), encoding="utf-8")
        return path

    def test_loads_and_interpolates_a_yaml_file(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            f"""
            station: {STATION}
            atproto:
              identifier: ${{ATP_IDENTIFIER}}
              password: ${{ATP_APP_PASSWORD}}
            mqtt:
              url: mqtt://broker.local
            sources:
              - topic: weather/temp
                field: temperature
                unit: fahrenheit
            """,
        )

        config = load_config(
            path, {"ATP_IDENTIFIER": "me.example.com", "ATP_APP_PASSWORD": "abcd-efgh-ijkl-mnop"}
        )

        assert config.atproto.identifier == "me.example.com"

    def test_does_not_interpolate_inside_comments(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            f"""
            # Set ${{ATP_APP_PASSWORD}} in your environment before running this.
            station: {STATION}
            atproto:
              identifier: me.example.com
              password: abcd-efgh-ijkl-mnop
            mqtt:
              url: mqtt://broker.local
            sources:
              - topic: weather/temp
                field: temperature
                unit: celsius
            """,
        )

        # The comment mentions a variable that is not set; it must not be read
        # as a substitution.
        assert load_config(path, {}).atproto.identifier == "me.example.com"

    def test_reports_a_missing_file_clearly(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="not found"):
            load_config(tmp_path / "nope.yaml")

    def test_reports_invalid_yaml_clearly(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, "station: [unclosed\n")
        with pytest.raises(ConfigError, match="not valid YAML"):
            load_config(path)


class TestAtprotoOnlySettings:
    def test_reads_credentials_from_the_environment(self) -> None:
        settings = load_atproto_settings(
            None,
            {"ATP_IDENTIFIER": "me.example.com", "ATP_APP_PASSWORD": "abcd-efgh-ijkl-mnop"},
        )

        assert settings.identifier == "me.example.com"
        assert settings.service == "https://bsky.social"

    def test_explains_what_is_missing(self) -> None:
        with pytest.raises(ConfigError, match="ATP_IDENTIFIER"):
            load_atproto_settings(None, {})

    def test_prefers_the_config_file_when_it_has_credentials(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(
            "atproto:\n  identifier: from-file.example.com\n  password: abcd-efgh-ijkl-mnop\n"
        )

        settings = load_atproto_settings(path, {"ATP_IDENTIFIER": "from-env.example.com"})

        assert settings.identifier == "from-file.example.com"
