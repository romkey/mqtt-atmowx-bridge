"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from . import __version__
from .atproto.client import AtpClient
from .atproto.errors import NetworkError, XrpcError
from .atproto.session import SessionError, SessionManager, looks_like_app_password
from .atproto.session_store import FileSessionStore
from .bridge.bridge import Bridge
from .config import (
    AtprotoSettings,
    Config,
    ConfigError,
    ResolvedJsonSource,
    load_atproto_settings,
    load_config,
)
from .health import HealthServer
from .logging_setup import BridgeLogger, configure_logging
from .observation.fields import EXTRA_PREFIX, FIELDS
from .observation.quantity import to_quantity
from .observation.units import DIMENSIONS, SI_UNIT, known_units, to_si
from .station import KINDS, STATUSES, StationInput, create_station, list_stations

DEFAULT_CONFIG = "config.yaml"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mqtt-atmowx-bridge",
        description="Publish weather observations from MQTT to the AT Protocol.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  mqtt-atmowx-bridge validate -c config.yaml\n"
            "  mqtt-atmowx-bridge station create --name 'Back Garden' --lat 45.52 --lon -122.68\n"
            "  mqtt-atmowx-bridge run -c config.yaml --dry-run\n"
            "  mqtt-atmowx-bridge convert --from fahrenheit --dimension temperature 68.5\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--env-file",
        metavar="PATH",
        help="load environment variables from this file (default: ./.env if present)",
    )

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-c", "--config", default=DEFAULT_CONFIG, metavar="PATH", help="path to the config file"
    )
    common.add_argument(
        "--log-level",
        choices=["trace", "debug", "info", "warn", "error", "fatal", "silent"],
        help="override log.level from the config",
    )
    common.add_argument("--pretty", action="store_true", help="human-readable logs instead of JSON")

    subcommands = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    run = subcommands.add_parser(
        "run", parents=[common], help="run the bridge (the default deployment mode)"
    )
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="build and log observations without publishing them",
    )
    run.add_argument(
        "--once",
        action="store_true",
        help="publish a single observation once readings arrive, then exit",
    )
    run.set_defaults(handler=command_run)

    validate = subcommands.add_parser(
        "validate", parents=[common], help="check the config without connecting to anything"
    )
    validate.set_defaults(handler=command_validate)

    station = subcommands.add_parser("station", help="create and list net.atmowx.station records")
    station_commands = station.add_subparsers(
        dest="station_command", required=True, metavar="<subcommand>"
    )

    station_create = station_commands.add_parser(
        "create", parents=[common], help="create a station record"
    )
    station_create.add_argument("--name", required=True, help="human-readable station name")
    station_create.add_argument(
        "--lat", "--latitude", dest="latitude", type=float, required=True, help="degrees north"
    )
    station_create.add_argument(
        "--lon", "--longitude", dest="longitude", type=float, required=True, help="degrees east"
    )
    station_create.add_argument(
        "--elevation", type=float, help="station elevation in meters above sea level"
    )
    station_create.add_argument("--description")
    station_create.add_argument("--kind", choices=KINDS)
    station_create.add_argument("--status", choices=STATUSES)
    station_create.add_argument("--hardware", help='e.g. "Ambient Weather WS-2902"')
    station_create.add_argument("--software", help='e.g. "mqtt-atmowx-bridge 0.1.0"')
    station_create.add_argument("--timezone", help='IANA zone, e.g. "America/Los_Angeles"')
    station_create.set_defaults(handler=command_station_create)

    station_list = station_commands.add_parser(
        "list", parents=[common], help="list your station records"
    )
    station_list.add_argument("--limit", type=int, default=50)
    station_list.set_defaults(handler=command_station_list)

    login = subcommands.add_parser(
        "login", parents=[common], help="authenticate and save a session"
    )
    login.set_defaults(handler=command_login)

    session = subcommands.add_parser(
        "session", parents=[common], help="show the saved session's state"
    )
    session.set_defaults(handler=command_session)

    logout = subcommands.add_parser(
        "logout", parents=[common], help="revoke and delete the saved session"
    )
    logout.set_defaults(handler=command_logout)

    fields = subcommands.add_parser("fields", help="list the publishable observation fields")
    fields.add_argument("--json", action="store_true", help="machine-readable output")
    fields.set_defaults(handler=command_fields)

    convert = subcommands.add_parser(
        "convert", help="convert a value to its SI unit, to check a mapping"
    )
    convert.add_argument("value", type=float)
    convert.add_argument("--from", dest="unit", required=True, help="the unit to convert from")
    convert.add_argument("--dimension", choices=DIMENSIONS, required=True)
    convert.add_argument("--decimals", type=int, default=2)
    convert.set_defaults(handler=command_convert)

    return parser


# -- helpers -----------------------------------------------------------------


def make_logger(args: argparse.Namespace, config: Config | None = None) -> BridgeLogger:
    level = getattr(args, "log_level", None) or (config.log.level if config else "info")
    pretty = getattr(args, "pretty", False) or (
        config.log.pretty if config else sys.stderr.isatty()
    )
    return configure_logging(level=level, pretty=pretty)


def build_session(
    settings: AtprotoSettings, logger: BridgeLogger
) -> tuple[SessionManager, AtpClient]:
    if not looks_like_app_password(settings.password):
        logger.warn(
            "the configured password does not look like an app password "
            "(xxxx-xxxx-xxxx-xxxx). Use an app password, not your account password."
        )

    session = SessionManager(
        service=settings.service,
        identifier=settings.identifier,
        password=settings.password,
        store=FileSessionStore(settings.session_file),
        logger=logger.bind("session"),
        refresh_skew_seconds=settings.refresh_skew_seconds,
        timeout_seconds=settings.request_timeout_seconds,
    )
    client = AtpClient(
        session, logger=logger.bind("atproto"), timeout_seconds=settings.request_timeout_seconds
    )
    return session, client


def print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, default=str))


# -- commands ----------------------------------------------------------------


def command_run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.dry_run:
        config = _with_dry_run(config)

    logger = make_logger(args, config)
    session, client = build_session(config.atproto, logger)

    bridge = Bridge(config=config, client=client, logger=logger)
    health: HealthServer | None = None

    if config.health.enabled:
        health = HealthServer(
            host=config.health.host,
            port=config.health.port,
            status=lambda: {**bridge.status(), "session": session.status()},
            logger=logger.bind("health"),
        )
        health.start()

    stopped = threading.Event()

    def shut_down(*_: Any) -> None:
        if stopped.is_set():
            return
        stopped.set()
        bridge.stop()
        if health is not None:
            health.stop()
        client.close()
        session.close()

    signal.signal(signal.SIGINT, lambda *_: shut_down())
    signal.signal(signal.SIGTERM, lambda *_: shut_down())

    bridge.start()

    if args.once:
        # Wait for the first readings to land, publish once, and stop. Useful
        # for a cron-style deployment or for checking a new config end to end.
        if not bridge.wait_until_connected(30):
            logger.error("could not connect to the MQTT broker within 30 seconds")
            shut_down()
            return 1
        deadline = threading.Event()
        deadline.wait(min(config.publish.interval_seconds, 30))
        bridge.publish_now("once")
        shut_down()
        return 0

    try:
        while not stopped.wait(1.0):
            pass
    except KeyboardInterrupt:
        shut_down()
    return 0


def _with_dry_run(config: Config) -> Config:
    from dataclasses import replace

    return replace(config, publish=config.publish.model_copy(update={"dry_run": True}))


def command_validate(args: argparse.Namespace) -> int:
    config = load_config(args.config)

    print(f"{args.config} is valid.\n")
    print(f"  station        {config.station}")
    print(f"  broker         {config.mqtt.url}")
    print(f"  pds            {config.atproto.service} as {config.atproto.identifier}")
    print(
        f"  publish        {config.publish.mode}"
        + (
            f" every {config.publish.interval_seconds}s"
            if config.publish.mode == "interval"
            else f", at most every {config.publish.min_interval_seconds}s"
        )
    )
    print(f"  subscriptions  {len(config.topics)}")
    for topic in config.topics:
        print(f"                   {topic}")

    print("\n  measurements")
    for source in config.sources:
        mappings = (
            [mapping for _, mapping in source.mappings]
            if isinstance(source, ResolvedJsonSource)
            else [source.mapping]
        )
        for mapping in mappings:
            unit = mapping.unit or "(none)"
            si = SI_UNIT[mapping.dimension]
            arrow = f"{unit} -> {si}" if unit != si else si
            print(f"    {mapping.name:<24} {arrow:<34} {source.topic}")

    enabled = [
        name
        for name, mode in (
            ("dewPoint", config.derive.dew_point),
            ("pressureSeaLevel", config.derive.pressure_sea_level),
            ("pressureStation", config.derive.pressure_station),
        )
        if mode != "never"
    ]
    if enabled:
        print(f"\n  derived        {', '.join(enabled)}")
    return 0


def command_station_create(args: argparse.Namespace) -> int:
    settings = load_atproto_settings(args.config)
    logger = make_logger(args)
    session, client = build_session(settings, logger)

    try:
        result = create_station(
            client,
            StationInput(
                name=args.name,
                latitude=args.latitude,
                longitude=args.longitude,
                elevation_meters=args.elevation,
                description=args.description,
                kind=args.kind,
                status=args.status,
                hardware=args.hardware,
                software=args.software,
                timezone=args.timezone,
            ),
        )
    finally:
        client.close()
        session.close()

    uri = result.get("uri")
    print(f"Created station {uri}")
    print("\nAdd this to your config:\n")
    print(f"station: {uri}")
    return 0


def command_station_list(args: argparse.Namespace) -> int:
    settings = load_atproto_settings(args.config)
    logger = make_logger(args)
    session, client = build_session(settings, logger)

    try:
        records = list_stations(client, limit=args.limit)
    finally:
        client.close()
        session.close()

    if not records:
        print("No station records yet. Create one with `mqtt-atmowx-bridge station create`.")
        return 0

    for record in records:
        value = record.get("value", {})
        print(f"{record.get('uri')}")
        print(f"  name        {value.get('name')}")
        if value.get("description"):
            print(f"  description {value['description']}")
        location = value.get("location", {})
        if location:
            print(
                f"  location    {_quantity_text(location.get('latitude'))}, "
                f"{_quantity_text(location.get('longitude'))}"
            )
        print()
    return 0


def _quantity_text(quantity: Any) -> str:
    if not isinstance(quantity, dict) or "value" not in quantity:
        return "?"
    return str(float(quantity["value"]) * 10 ** float(quantity.get("scale", 0)))


def command_login(args: argparse.Namespace) -> int:
    settings = load_atproto_settings(args.config)
    logger = make_logger(args)
    session, client = build_session(settings, logger)

    try:
        current = session.session()
        print(f"Logged in as {current.handle} ({current.did})")
        print(f"  pds     {current.pds_url}")
        print(f"  session {settings.session_file}")
        print(f"  expires {current.access_expires_at}")
    finally:
        client.close()
        session.close()
    return 0


def command_session(args: argparse.Namespace) -> int:
    settings = load_atproto_settings(args.config)
    store = FileSessionStore(settings.session_file)
    stored = store.load()

    if stored is None:
        print(f"No saved session at {settings.session_file}.")
        print("Run `mqtt-atmowx-bridge login` to create one.")
        return 1

    print(f"Session for {stored.handle} ({stored.did})")
    print(f"  pds              {stored.pds_url}")
    print(f"  saved            {stored.saved_at}")
    print(f"  access expires   {stored.access_expires_at}")
    print(f"  refresh expires  {stored.refresh_expires_at}")
    return 0


def command_logout(args: argparse.Namespace) -> int:
    settings = load_atproto_settings(args.config)
    logger = make_logger(args)
    session, client = build_session(settings, logger)

    try:
        session.logout()
        print(f"Session revoked and {settings.session_file} removed.")
    finally:
        client.close()
        session.close()
    return 0


def command_fields(args: argparse.Namespace) -> int:
    if args.json:
        print_json(
            [
                {
                    "name": name,
                    "path": spec.path,
                    "dimension": spec.dimension,
                    "unit": SI_UNIT[spec.dimension],
                    "decimals": spec.decimals,
                    "description": spec.description,
                    "accepts": known_units(spec.dimension),
                }
                for name, spec in FIELDS.items()
            ]
        )
        return 0

    print("Observation fields (use these as `field:` in a mapping)\n")
    for name, spec in FIELDS.items():
        print(f"  {name:<24} {SI_UNIT[spec.dimension]:<24} {spec.description}")
    print(
        f'\nAnything the lexicon does not cover goes in `extra` as "{EXTRA_PREFIX}<parameter>", '
        "\ne.g. `field: extra:leafWetness`. Its dimension is inferred from the unit you give it."
    )
    print("\nAccepted unit spellings per dimension:\n")
    for dimension in DIMENSIONS:
        print(f"  {dimension:<18} {', '.join(known_units(dimension))}")
    return 0


def command_convert(args: argparse.Namespace) -> int:
    si = to_si(args.dimension, args.unit, args.value)
    quantity = to_quantity(si, args.decimals)
    print(f"{args.value} {args.unit} = {si:.6g} {SI_UNIT[args.dimension]}")
    print(f"encoded: {json.dumps(quantity)}")
    return 0


# -- entry point -------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.env_file:
        load_dotenv(args.env_file, override=False)
    elif Path(".env").exists():
        load_dotenv(".env", override=False)

    try:
        handler = args.handler
        return int(handler(args))
    except ConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except (XrpcError, SessionError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 3
    except NetworkError as error:
        print(f"error: could not reach the service: {error}", file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
