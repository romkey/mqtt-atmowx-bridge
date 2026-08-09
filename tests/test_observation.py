"""Encoding: TIDs, quantities, unit conversion, record assembly."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from mqtt_atmowx_bridge.atproto.tid import (
    TID_PATTERN,
    datetime_from_tid,
    decode_tid,
    encode_tid,
    now_tid,
    tid_from_datetime,
)
from mqtt_atmowx_bridge.observation.derive import (
    dew_point_celsius,
    sea_level_pressure_hpa,
    station_pressure_hpa,
)
from mqtt_atmowx_bridge.observation.fields import CoreTarget, ExtraTarget, parse_target
from mqtt_atmowx_bridge.observation.quantity import from_quantity, to_quantity
from mqtt_atmowx_bridge.observation.record import Reading, build_observation
from mqtt_atmowx_bridge.observation.units import to_si

STATION = "at://did:plc:abc123yourdid/net.atmowx.station/3mremies2t222"
WHEN = datetime(2026, 8, 7, 21, 35, 0, tzinfo=UTC)


def reading(name: str, value: float, dimension: str = "temperature", decimals: int = 1) -> Reading:
    return Reading(
        target=CoreTarget(field=name),
        value=value,
        dimension=dimension,  # type: ignore[arg-type]
        decimals=decimals,
        observed_at=WHEN,
        received_at=WHEN,
        source="test",
    )


class TestTid:
    def test_is_13_characters_of_the_sortable_alphabet(self) -> None:
        tid = tid_from_datetime(WHEN)
        assert len(tid) == 13
        assert TID_PATTERN.match(tid)

    def test_round_trips_through_the_encoding(self) -> None:
        microseconds = 1_754_602_500_000_000
        assert decode_tid(encode_tid(microseconds, 7)) == (microseconds, 7)

    def test_is_deterministic_so_republishing_overwrites(self) -> None:
        assert tid_from_datetime(WHEN) == tid_from_datetime(WHEN)

    def test_sorts_chronologically_as_a_string(self) -> None:
        moments = [
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 6, 1, tzinfo=UTC),
            datetime(2026, 8, 7, 21, 35, tzinfo=UTC),
        ]
        tids = [tid_from_datetime(moment) for moment in moments]
        assert tids == sorted(tids)

    def test_decodes_back_to_the_observation_time(self) -> None:
        assert datetime_from_tid(tid_from_datetime(WHEN)) == WHEN

    def test_now_tid_uses_a_random_clock_id_per_the_spec(self) -> None:
        # 100 draws from 1024 values collide sometimes; distinct-ish is enough.
        assert len({now_tid() for _ in range(50)}) > 1

    def test_rejects_a_naive_datetime(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            tid_from_datetime(datetime(2026, 8, 7, 21, 35))

    def test_rejects_malformed_input(self) -> None:
        with pytest.raises(ValueError, match="not a TID"):
            decode_tid("nope")


class TestQuantity:
    @pytest.mark.parametrize(
        ("value", "decimals", "expected"),
        [
            (28.1, 1, {"value": 281, "scale": -1}),
            (1011.2, 1, {"value": 10112, "scale": -1}),
            (34.0, 0, {"value": 34}),
            (0.0, 1, {"value": 0}),
            (-5.25, 2, {"value": -525, "scale": -2}),
            (7.0, 1, {"value": 7}),
            (784.0, 0, {"value": 784}),
        ],
    )
    def test_encodes_at_the_requested_precision(
        self, value: float, decimals: int, expected: dict[str, int]
    ) -> None:
        assert to_quantity(value, decimals) == expected

    def test_drops_trailing_zeros_rather_than_implying_precision(self) -> None:
        assert to_quantity(1.0, 3) == {"value": 1}
        assert to_quantity(1.50, 3) == {"value": 15, "scale": -1}

    def test_rounds_halves_away_from_zero(self) -> None:
        assert to_quantity(0.25, 1) == {"value": 3, "scale": -1}
        assert to_quantity(-0.25, 1) == {"value": -3, "scale": -1}

    def test_round_trips(self) -> None:
        assert from_quantity(to_quantity(1013.25, 2)) == pytest.approx(1013.25)

    def test_rejects_a_value_that_is_not_finite(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            to_quantity(float("nan"), 1)

    def test_rejects_absurd_precision(self) -> None:
        with pytest.raises(ValueError, match=r"0\.\.9"):
            to_quantity(1.0, 12)


class TestUnitConversion:
    @pytest.mark.parametrize(
        ("dimension", "unit", "value", "expected"),
        [
            ("temperature", "fahrenheit", 68.0, 20.0),
            ("temperature", "F", 32.0, 0.0),
            ("temperature", "kelvin", 273.15, 0.0),
            ("temperature", "celsius", 21.5, 21.5),
            ("pressure", "inHg", 29.92, 1013.21),
            ("pressure", "pascal", 101325, 1013.25),
            ("speed", "mph", 10.0, 4.4704),
            ("speed", "km/h", 36.0, 10.0),
            ("speed", "knots", 1.0, 0.514444),
            ("length", "inches", 1.0, 25.4),
            ("precipitationRate", "in/hr", 1.0, 25.4),
            ("distance", "miles", 1.0, 1609.344),
            ("ratio", "fraction", 0.34, 34.0),
            ("angle", "degrees", 315.0, 315.0),
        ],
    )
    def test_converts_to_si(self, dimension: str, unit: str, value: float, expected: float) -> None:
        assert to_si(dimension, unit, value) == pytest.approx(expected, rel=1e-4)  # type: ignore[arg-type]

    def test_spelling_and_case_do_not_matter(self) -> None:
        for spelling in ("metersPerSecond", "m/s", "MPS", " M/S "):
            assert to_si("speed", spelling, 5.0) == 5.0

    def test_rejects_a_unit_from_the_wrong_dimension(self) -> None:
        with pytest.raises(ValueError, match="unknown speed unit"):
            to_si("speed", "inHg", 1.0)


class TestFieldTargets:
    def test_resolves_a_core_field(self) -> None:
        assert parse_target("wind.speed") == CoreTarget(field="wind.speed")

    def test_resolves_an_extra_measurement(self) -> None:
        assert parse_target("extra:leafWetness") == ExtraTarget(parameter="leafWetness")

    def test_rejects_an_unknown_field_with_a_helpful_message(self) -> None:
        with pytest.raises(ValueError, match="unknown observation field"):
            parse_target("temprature")


class TestBuildObservation:
    def test_builds_the_record_from_the_lexicon_example(self) -> None:
        readings = {
            "temperature": reading("temperature", 28.1),
            "dewPoint": reading("dewPoint", 11.8),
            "relativeHumidity": reading("relativeHumidity", 34, "ratio", 0),
            "pressureStation": reading("pressureStation", 1011.2, "pressure", 1),
            "pressureSeaLevel": reading("pressureSeaLevel", 1013.1, "pressure", 1),
            "wind.speed": reading("wind.speed", 2.7, "speed", 1),
            "wind.gust": reading("wind.gust", 5.8, "speed", 1),
            "wind.direction": reading("wind.direction", 315, "angle", 0),
            "precipitation.rate": reading("precipitation.rate", 0, "precipitationRate", 1),
            "precipitation.day": reading("precipitation.day", 0, "length", 1),
            "solarIrradiance": reading("solarIrradiance", 784, "irradiance", 0),
            "uvIndex": reading("uvIndex", 7, "dimensionless", 0),
        }

        result = build_observation(station=STATION, observed_at=WHEN, readings=readings)

        assert result.record == {
            "$type": "net.atmowx.observation",
            "station": STATION,
            "observedAt": "2026-08-07T21:35:00.000Z",
            "temperature": {"value": 281, "scale": -1},
            "dewPoint": {"value": 118, "scale": -1},
            "relativeHumidity": {"value": 34},
            "pressureStation": {"value": 10112, "scale": -1},
            "pressureSeaLevel": {"value": 10131, "scale": -1},
            "wind": {
                "speed": {"value": 27, "scale": -1},
                "gust": {"value": 58, "scale": -1},
                "direction": 315,
            },
            "precipitation": {"rate": {"value": 0}, "day": {"value": 0}},
            "solarIrradiance": {"value": 784},
            "uvIndex": {"value": 7},
        }
        assert result.rejected == []

    def test_publishes_only_the_fields_it_received(self) -> None:
        result = build_observation(
            station=STATION,
            observed_at=WHEN,
            readings={"temperature": reading("temperature", 21.5)},
        )

        assert set(result.record) == {"$type", "station", "observedAt", "temperature"}
        assert "wind" not in result.record
        assert "relativeHumidity" not in result.record

    def test_orders_fields_by_the_lexicon_not_arrival(self) -> None:
        result = build_observation(
            station=STATION,
            observed_at=WHEN,
            readings={
                "uvIndex": reading("uvIndex", 4, "dimensionless", 0),
                "temperature": reading("temperature", 21.5),
                "relativeHumidity": reading("relativeHumidity", 55, "ratio", 0),
            },
        )
        assert list(result.record)[3:] == ["temperature", "relativeHumidity", "uvIndex"]

    def test_rejects_an_implausible_reading_and_keeps_the_rest(self) -> None:
        result = build_observation(
            station=STATION,
            observed_at=WHEN,
            readings={
                "temperature": reading("temperature", 21.5),
                "relativeHumidity": reading("relativeHumidity", 214, "ratio", 0),
            },
        )

        assert "relativeHumidity" not in result.record
        assert result.included == ["temperature"]
        assert result.rejected[0]["name"] == "relativeHumidity"
        assert "maximum" in result.rejected[0]["reason"]

    def test_wraps_a_wind_direction_of_360_to_0(self) -> None:
        result = build_observation(
            station=STATION,
            observed_at=WHEN,
            readings={"wind.direction": reading("wind.direction", 360, "angle", 0)},
        )
        assert result.record["wind"]["direction"] == 0

    def test_rounds_a_direction_of_359_7_without_leaving_the_valid_range(self) -> None:
        result = build_observation(
            station=STATION,
            observed_at=WHEN,
            readings={"wind.direction": reading("wind.direction", 359.7, "angle", 0)},
        )
        assert result.record["wind"]["direction"] == 0

    def test_puts_unmapped_measurements_in_extra(self) -> None:
        readings = {
            "extra:leafWetness": Reading(
                target=ExtraTarget(parameter="leafWetness"),
                value=42,
                dimension="dimensionless",
                decimals=0,
                observed_at=WHEN,
                received_at=WHEN,
                source="test",
                height=1.5,
            )
        }
        result = build_observation(station=STATION, observed_at=WHEN, readings=readings)

        assert result.record["extra"] == [
            {
                "$type": "net.atmowx.defs#measurement",
                "parameter": "leafWetness",
                "value": {"value": 42},
                "unit": "index",
                "height": {"value": 15, "scale": -1},
            }
        ]

    def test_formats_observed_at_with_milliseconds_in_utc(self) -> None:
        result = build_observation(
            station=STATION,
            observed_at=datetime(2026, 8, 7, 21, 35, 12, 340000, tzinfo=UTC),
            readings={"temperature": reading("temperature", 21.5)},
        )
        assert result.record["observedAt"] == "2026-08-07T21:35:12.340Z"


class TestDerivedValues:
    def test_dew_point_matches_the_magnus_formula(self) -> None:
        assert dew_point_celsius(21.5, 50) == pytest.approx(10.63, abs=0.05)
        assert dew_point_celsius(30, 90) == pytest.approx(28.18, abs=0.05)

    def test_dew_point_equals_temperature_at_saturation(self) -> None:
        assert dew_point_celsius(15, 100) == pytest.approx(15, abs=0.01)

    def test_sea_level_pressure_is_higher_than_station_pressure(self) -> None:
        reduced = sea_level_pressure_hpa(950, 500, 15)
        assert reduced > 950
        assert reduced == pytest.approx(1007.7, abs=0.5)

    def test_the_pressure_reductions_invert_each_other(self) -> None:
        assert station_pressure_hpa(sea_level_pressure_hpa(950, 500, 15), 500, 15) == pytest.approx(
            950, abs=0.01
        )

    def test_the_pressure_reduction_grows_with_elevation(self) -> None:
        at_sea_level = sea_level_pressure_hpa(1000, 0, 15)
        on_a_hill = sea_level_pressure_hpa(1000, 200, 15)

        assert at_sea_level == pytest.approx(1000, abs=0.01)
        assert on_a_hill > at_sea_level
