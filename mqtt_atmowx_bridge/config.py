"""Config loading: a YAML file describing which MQTT topics carry which
measurement, in what units, for which station.

Secrets belong in the environment, so ``${VAR}`` and ``${VAR:-fallback}`` are
interpolated into string values before validation. Interpolation happens on the
*parsed* document rather than the raw text, so a ``${VAR}`` mentioned in a
comment stays a comment.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .observation.fields import Target, field_spec, parse_target
from .observation.units import Dimension, dimensions_for_unit, is_known_unit, known_units

ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

AT_URI_PATTERN = re.compile(
    r"^at://did:[a-z0-9]+:[A-Za-z0-9._:%-]+/[A-Za-z0-9.]+/[A-Za-z0-9._~-]+$"
)

MQTT_SCHEMES = {"mqtt", "mqtts", "ws", "wss", "tcp", "ssl"}

DeriveMode = Literal["never", "whenMissing", "always"]


class ConfigError(Exception):
    """The config file is missing, unreadable, or invalid."""


def interpolate_env(text: str, env: dict[str, str] | None = None) -> str:
    """Substitute ``${VAR}`` and ``${VAR:-fallback}`` in one string."""
    environment = os.environ if env is None else env

    def replace(match: re.Match[str]) -> str:
        name, fallback = match.group(1), match.group(2)
        value = environment.get(name)
        if value:
            return value
        if fallback is not None:
            return fallback
        raise ConfigError(f"environment variable {name} is referenced by the config but is not set")

    return ENV_PATTERN.sub(replace, text)


def interpolate_document(value: Any, env: dict[str, str] | None = None) -> Any:
    """Interpolate every string in a parsed YAML document."""
    if isinstance(value, str):
        return interpolate_env(value, env)
    if isinstance(value, list):
        return [interpolate_document(item, env) for item in value]
    if isinstance(value, dict):
        return {key: interpolate_document(item, env) for key, item in value.items()}
    return value


def _to_camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(word.capitalize() for word in rest)


class Base(BaseModel):
    """Strict by default: a misspelled key is an error, not a silent no-op."""

    model_config = ConfigDict(
        extra="forbid",
        alias_generator=_to_camel,
        populate_by_name=True,
        frozen=True,
    )


class FieldMapping(Base):
    """One incoming value: where it goes, what unit it is in, how to clean it up."""

    #: Observation field name, or ``extra:<parameter>``.
    field: str = Field(min_length=1)
    #: Unit of the incoming value; converted to the field's SI unit.
    unit: str = ""
    #: Decimal places to keep; defaults to the field's own precision.
    decimals: int | None = Field(default=None, ge=0, le=9)
    #: Linear calibration applied to the raw value before conversion.
    multiplier: float = 1.0
    offset: float = 0.0
    #: Ignore readings outside this range (before conversion).
    ignore_below: float | None = None
    ignore_above: float | None = None
    #: For ``extra:`` measurements: sensor height relative to ground, in meters.
    height: float | None = None


class TimestampSpec(Base):
    #: Dotted path to the timestamp inside a JSON payload.
    path: str = Field(min_length=1)
    format: Literal["iso", "epochSeconds", "epochMillis"] = "iso"


class JsonSource(Base):
    topic: str = Field(min_length=1)
    payload: Literal["json"]
    #: Sub-object to read the fields from, e.g. ``state.reported``.
    root: str | None = None
    timestamp: TimestampSpec | None = None
    #: JSON key (dotted path) -> mapping.
    map: dict[str, FieldMapping]
    #: In ``onMessage`` publish mode, a message here completes an observation.
    trigger: bool = False
    ignore_retained: bool = False

    @field_validator("map")
    @classmethod
    def _map_not_empty(cls, value: dict[str, FieldMapping]) -> dict[str, FieldMapping]:
        if not value:
            raise ValueError('"map" is empty, so this source would never produce a reading')
        return value


class ScalarSource(FieldMapping):
    """A topic whose whole payload is one value."""

    topic: str = Field(min_length=1)
    payload: Literal["number", "text"]
    trigger: bool = False
    ignore_retained: bool = False


Source = Annotated[JsonSource | ScalarSource, Field(discriminator="payload")]


class AtprotoSettings(Base):
    service: str = "https://bsky.social"
    identifier: str = Field(min_length=1)
    password: str = Field(min_length=1)
    session_file: str = "./data/session.json"
    #: Refresh this many seconds before the access token expires.
    refresh_skew_seconds: int = Field(default=300, ge=0, le=3600)
    request_timeout_seconds: int = Field(default=30, ge=1, le=300)

    @field_validator("service")
    @classmethod
    def _http_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"must be an http(s) URL, got {value!r}")
        return value


class TlsSettings(Base):
    ca: str | None = None
    cert: str | None = None
    key: str | None = None
    reject_unauthorized: bool = True


class MqttSettings(Base):
    url: str
    username: str | None = None
    password: str | None = None
    client_id: str | None = None
    qos: Literal[0, 1, 2] = 0
    keepalive_seconds: int = Field(default=60, ge=0, le=65535)
    reconnect_period_seconds: float = Field(default=5.0, ge=0, le=3600)
    connect_timeout_seconds: float = Field(default=30.0, ge=1, le=300)
    clean: bool = True
    protocol_version: Literal[4, 5] = 4
    tls: TlsSettings | None = None

    @field_validator("url")
    @classmethod
    def _mqtt_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in MQTT_SCHEMES or not parsed.hostname:
            raise ValueError(f"must be an mqtt://, mqtts://, ws:// or wss:// URL, got {value!r}")
        return value

    @field_validator("username", "password", "client_id")
    @classmethod
    def _empty_is_unset(cls, value: str | None) -> str | None:
        # `${MQTT_USERNAME:-}` in a config for an anonymous broker resolves to
        # an empty string; treat that as "not set" rather than logging in as "".
        return value or None


class PublishSettings(Base):
    #: ``interval`` publishes on a timer; ``onMessage`` when a trigger topic arrives.
    mode: Literal["interval", "onMessage"] = "interval"
    interval_seconds: int = Field(default=300, ge=5, le=86400)
    #: Line observations up on interval boundaries, so keys are tidy.
    align_to_interval: bool = True
    #: Floor between records in ``onMessage`` mode.
    min_interval_seconds: int = Field(default=60, ge=0, le=86400)
    #: Readings older than this are not published.
    max_reading_age_seconds: int = Field(default=900, ge=1, le=86400)
    #: Do not publish a record carrying fewer than this many measurements.
    min_fields: int = Field(default=1, ge=1, le=64)
    #: Skip a record whose measurements are identical to the last one published.
    skip_unchanged: bool = False
    #: ``message`` uses the payload's own timestamp; ``received`` uses arrival time.
    timestamp_source: Literal["message", "received"] = "message"
    #: Round ``observedAt`` down to a multiple of this many seconds.
    round_to_seconds: int = Field(default=1, ge=0, le=3600)
    #: Build and log records without writing them.
    dry_run: bool = False
    #: Records held while the PDS is unreachable.
    queue_size: int = Field(default=100, ge=0, le=10000)


class DeriveSettings(Base):
    dew_point: DeriveMode = "never"
    pressure_sea_level: DeriveMode = "never"
    pressure_station: DeriveMode = "never"
    #: Station elevation in meters; required for the pressure reductions.
    elevation_meters: float | None = Field(default=None, ge=-500, le=9000)


class HealthSettings(Base):
    enabled: bool = False
    port: int = Field(default=8080, ge=1, le=65535)
    host: str = "0.0.0.0"


class LogSettings(Base):
    level: Literal["trace", "debug", "info", "warn", "error", "fatal", "silent"] = "info"
    pretty: bool = False


class ConfigModel(Base):
    station: str
    atproto: AtprotoSettings
    mqtt: MqttSettings
    publish: PublishSettings = PublishSettings()
    derive: DeriveSettings = DeriveSettings()
    health: HealthSettings = HealthSettings()
    log: LogSettings = LogSettings()
    sources: list[Source] = Field(min_length=1)

    @field_validator("station")
    @classmethod
    def _at_uri(cls, value: str) -> str:
        if not AT_URI_PATTERN.match(value):
            raise ValueError(
                f"must be an AT-URI like at://did:plc:…/net.atmowx.station/…, got {value!r}"
            )
        return value

    @field_validator("sources", mode="before")
    @classmethod
    def _default_payload(cls, value: Any) -> Any:
        # A source without `payload` is a bare numeric topic. Filling the
        # discriminator in here keeps validation errors pointed at the source
        # the user actually wrote.
        if not isinstance(value, list):
            return value
        return [
            {**item, "payload": "number"}
            if isinstance(item, dict) and "payload" not in item
            else item
            for item in value
        ]


# -- resolved (post-validation) shapes ---------------------------------------


@dataclass(frozen=True, slots=True)
class ResolvedMapping:
    """A mapping resolved against the field registry, ready to convert values."""

    #: Config-facing name, used as the reading key: ``temperature``, ``extra:leafWetness``.
    name: str
    target: Target
    dimension: Dimension
    unit: str
    decimals: int
    multiplier: float
    offset: float
    ignore_below: float | None
    ignore_above: float | None
    height: float | None


@dataclass(frozen=True, slots=True)
class ResolvedJsonSource:
    topic: str
    root: str | None
    timestamp: TimestampSpec | None
    #: ``(json path, mapping)`` pairs.
    mappings: tuple[tuple[str, ResolvedMapping], ...]
    trigger: bool
    ignore_retained: bool
    kind: str = "json"


@dataclass(frozen=True, slots=True)
class ResolvedScalarSource:
    topic: str
    payload: str
    mapping: ResolvedMapping
    trigger: bool
    ignore_retained: bool
    kind: str = "scalar"


ResolvedSource = ResolvedJsonSource | ResolvedScalarSource


@dataclass(frozen=True, slots=True)
class Config:
    """A validated config with its sources resolved against the field registry."""

    station: str
    atproto: AtprotoSettings
    mqtt: MqttSettings
    publish: PublishSettings
    derive: DeriveSettings
    health: HealthSettings
    log: LogSettings
    sources: tuple[ResolvedSource, ...]
    #: Every distinct topic filter to subscribe to.
    topics: tuple[str, ...]


def _resolve_mapping(raw: FieldMapping, where: str) -> ResolvedMapping:
    try:
        target = parse_target(raw.field)
    except ValueError as error:
        raise ConfigError(f"{where}: {error}") from error

    if target.kind == "core":
        dimension = field_spec(target.field).dimension  # type: ignore[union-attr]
    else:
        dimension = _dimension_for_unit(raw.unit, where)

    unit = raw.unit if raw.unit else _default_unit_for(dimension, raw.field, where)
    if not is_known_unit(dimension, unit):
        raise ConfigError(
            f'{where}: "{unit}" is not a {dimension} unit. {raw.field} is measured in '
            f"{dimension}; accepted spellings: {', '.join(known_units(dimension))}"
        )

    decimals = raw.decimals
    if decimals is None:
        decimals = field_spec(target.field).decimals if target.kind == "core" else 2  # type: ignore[union-attr]

    return ResolvedMapping(
        name=raw.field,
        target=target,
        dimension=dimension,
        unit=unit,
        decimals=decimals,
        multiplier=raw.multiplier,
        offset=raw.offset,
        ignore_below=raw.ignore_below,
        ignore_above=raw.ignore_above,
        height=raw.height,
    )


def _dimension_for_unit(unit: str, where: str) -> Dimension:
    """``extra:`` measurements have no declared dimension, so infer it from the unit.

    That also decides which SI unit they are published in.
    """
    if not unit:
        return "dimensionless"
    matches = dimensions_for_unit(unit)
    if not matches:
        raise ConfigError(f'{where}: unrecognized unit "{unit}"')
    if len(matches) > 1:
        # `m` and `mm` are both lengths and distances; ask rather than guess.
        raise ConfigError(
            f'{where}: unit "{unit}" is ambiguous for an extra measurement (it could be '
            f'{" or ".join(matches)}). Use an unambiguous spelling, e.g. "millimeters" '
            'or "meters".'
        )
    return matches[0]


def _default_unit_for(dimension: Dimension, field_name: str, where: str) -> str:
    if dimension == "dimensionless":
        return ""
    raise ConfigError(
        f'{where}: {field_name} needs a "unit"; it is a {dimension} measurement '
        f"(one of: {', '.join(known_units(dimension))})"
    )


def assert_valid_topic_filter(topic_filter: str, where: str) -> None:
    """MQTT topic filters: ``+`` spans one level, ``#`` only ever appears last."""
    if not topic_filter:
        raise ConfigError(f"{where}: topic filter is empty")
    levels = topic_filter.split("/")
    for index, level in enumerate(levels):
        if "+" in level and level != "+":
            raise ConfigError(f'{where}: "+" must occupy a whole topic level')
        if "#" in level:
            if level != "#":
                raise ConfigError(f'{where}: "#" must occupy a whole topic level')
            if index != len(levels) - 1:
                raise ConfigError(f'{where}: "#" is only allowed as the last topic level')


def _resolve_sources(sources: list[Any]) -> tuple[ResolvedSource, ...]:
    resolved: list[ResolvedSource] = []
    for index, source in enumerate(sources):
        where = f"sources[{index}] ({source.topic})"
        assert_valid_topic_filter(source.topic, where)

        if isinstance(source, JsonSource):
            mappings = tuple(
                (path, _resolve_mapping(mapping, f'{where} map."{path}"'))
                for path, mapping in source.map.items()
            )
            resolved.append(
                ResolvedJsonSource(
                    topic=source.topic,
                    root=source.root,
                    timestamp=source.timestamp,
                    mappings=mappings,
                    trigger=source.trigger,
                    ignore_retained=source.ignore_retained,
                )
            )
        else:
            resolved.append(
                ResolvedScalarSource(
                    topic=source.topic,
                    payload=source.payload,
                    mapping=_resolve_mapping(source, where),
                    trigger=source.trigger,
                    ignore_retained=source.ignore_retained,
                )
            )
    return tuple(resolved)


def _mapped_names(sources: tuple[ResolvedSource, ...]) -> set[str]:
    names: set[str] = set()
    for source in sources:
        if isinstance(source, ResolvedJsonSource):
            names.update(mapping.name for _, mapping in source.mappings)
        else:
            names.add(source.mapping.name)
    return names


def _assert_derive_is_satisfiable(
    derive: DeriveSettings, sources: tuple[ResolvedSource, ...]
) -> None:
    needs_elevation = derive.pressure_sea_level != "never" or derive.pressure_station != "never"
    if needs_elevation and derive.elevation_meters is None:
        raise ConfigError(
            "derive.elevationMeters is required to reduce pressure to sea level (or back again)"
        )
    if derive.pressure_sea_level != "never" and derive.pressure_station != "never":
        raise ConfigError(
            "derive.pressureSeaLevel and derive.pressureStation cannot both be enabled; "
            "pick the one your station does not measure"
        )

    mapped = _mapped_names(sources)

    def requires(what: str, needed: list[str]) -> None:
        missing = [name for name in needed if name not in mapped]
        if missing:
            raise ConfigError(
                f"derive.{what} needs {' and '.join(missing)}, which no source provides"
            )

    if derive.dew_point != "never":
        requires("dewPoint", ["temperature", "relativeHumidity"])
    if derive.pressure_sea_level != "never":
        requires("pressureSeaLevel", ["pressureStation", "temperature"])
    if derive.pressure_station != "never":
        requires("pressureStation", ["pressureSeaLevel", "temperature"])


def _assert_keys_stay_distinct(publish: PublishSettings) -> None:
    """Record keys come from ``observedAt`` rounded to ``roundToSeconds``.

    If we publish more often than we round, consecutive records land on the same
    key and silently overwrite each other — data loss that looks like everything
    working.
    """
    if publish.round_to_seconds == 0:
        return

    gap = publish.interval_seconds if publish.mode == "interval" else publish.min_interval_seconds
    if gap == 0 or publish.round_to_seconds <= gap:
        return

    setting = "intervalSeconds" if publish.mode == "interval" else "minIntervalSeconds"
    raise ConfigError(
        f"publish.roundToSeconds ({publish.round_to_seconds}) is longer than publish.{setting} "
        f"({gap}). Observation record keys are derived from the rounded time, so records "
        "published within the same rounding window would overwrite each other. Lower "
        f"roundToSeconds or raise {setting}."
    )


def parse_config(document: Any) -> Config:
    """Validate a config document and resolve its sources."""
    try:
        model = ConfigModel.model_validate(document)
    except ValidationError as error:
        raise ConfigError(_format_validation_error(error)) from error

    sources = _resolve_sources(model.sources)
    _assert_derive_is_satisfiable(model.derive, sources)
    _assert_keys_stay_distinct(model.publish)

    if model.publish.mode == "onMessage" and not any(source.trigger for source in sources):
        raise ConfigError(
            'publish.mode is "onMessage" but no source is marked `trigger: true`, '
            "so nothing would ever be published"
        )

    topics = tuple(dict.fromkeys(source.topic for source in sources))
    return Config(
        station=model.station,
        atproto=model.atproto,
        mqtt=model.mqtt,
        publish=model.publish,
        derive=model.derive,
        health=model.health,
        log=model.log,
        sources=sources,
        topics=topics,
    )


def _format_validation_error(error: ValidationError) -> str:
    lines = []
    for issue in error.errors():
        location = ".".join(str(part) for part in issue["loc"]) or "(root)"
        lines.append(f"  {location}: {issue['msg']}")
    return "config is not valid:\n" + "\n".join(lines)


def load_config(path: str | os.PathLike[str], env: dict[str, str] | None = None) -> Config:
    """Read, interpolate and validate a config file."""
    absolute = Path(path).expanduser().resolve()
    try:
        text = absolute.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise ConfigError(f"config file not found: {absolute}") from error

    try:
        document = interpolate_document(yaml.safe_load(text), env)
    except yaml.YAMLError as error:
        raise ConfigError(f"{absolute} is not valid YAML: {error}") from error

    try:
        return parse_config(document)
    except ConfigError as error:
        raise ConfigError(f"{absolute}: {error}") from error


def load_atproto_settings(
    path: str | os.PathLike[str] | None, env: dict[str, str] | None = None
) -> AtprotoSettings:
    """Credentials only, for commands that run before a station exists.

    There is no station AT-URI to put in the config until ``station create`` has
    printed one, so those commands read the ``atproto`` section on its own and
    fall back to the environment.
    """
    environment = os.environ if env is None else env

    if path is not None:
        candidate = Path(path).expanduser()
        if candidate.exists():
            document = yaml.safe_load(candidate.read_text(encoding="utf-8"))
            if isinstance(document, dict) and document.get("atproto") is not None:
                try:
                    return AtprotoSettings.model_validate(
                        interpolate_document(document["atproto"], env)
                    )
                except ValidationError as error:
                    raise ConfigError(_format_validation_error(error)) from error

    identifier = environment.get("ATP_IDENTIFIER")
    password = environment.get("ATP_APP_PASSWORD")
    if not identifier or not password:
        raise ConfigError(
            "no atproto credentials: set ATP_IDENTIFIER and ATP_APP_PASSWORD, or point "
            "--config at a file with an `atproto` section"
        )

    settings: dict[str, Any] = {"identifier": identifier, "password": password}
    if environment.get("ATP_SERVICE"):
        settings["service"] = environment["ATP_SERVICE"]
    if environment.get("ATP_SESSION_FILE"):
        settings["sessionFile"] = environment["ATP_SESSION_FILE"]
    return AtprotoSettings.model_validate(settings)
