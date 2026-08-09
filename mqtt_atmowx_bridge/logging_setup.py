"""Structured logging with a small, deliberate API.

Every log line is a message plus keyword fields. Tokens and passwords are
redacted here rather than at each call site, so an accidental
``log.info("...", session=session)`` cannot leak a credential.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any, ClassVar

REDACTED = "[redacted]"

#: Field names whose values never reach the log, at any nesting depth.
SENSITIVE_KEYS = frozenset(
    {
        "password",
        "app_password",
        "accessjwt",
        "access_jwt",
        "refreshjwt",
        "refresh_jwt",
        "authorization",
        "token",
        "secret",
    }
)

LEVELS = ("trace", "debug", "info", "warn", "error", "fatal", "silent")


def _redact(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return value
    if isinstance(value, dict):
        return {
            key: (REDACTED if str(key).lower() in SENSITIVE_KEYS else _redact(item, depth + 1))
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [_redact(item, depth + 1) for item in value]
    return value


class BridgeLogger:
    """A logger that takes structured fields as keyword arguments."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def bind(self, name: str) -> BridgeLogger:
        return BridgeLogger(self._logger.getChild(name))

    def debug(self, message: str, **fields: Any) -> None:
        self._emit(logging.DEBUG, message, fields)

    def info(self, message: str, **fields: Any) -> None:
        self._emit(logging.INFO, message, fields)

    def warn(self, message: str, **fields: Any) -> None:
        self._emit(logging.WARNING, message, fields)

    warning = warn

    def error(self, message: str, **fields: Any) -> None:
        self._emit(logging.ERROR, message, fields)

    def exception(self, message: str, **fields: Any) -> None:
        self._logger.error(message, exc_info=True, extra={"fields": _redact(fields)})

    def _emit(self, level: int, message: str, fields: dict[str, Any]) -> None:
        if not self._logger.isEnabledFor(level):
            return
        clean = {key: value for key, value in fields.items() if value is not None}
        self._logger.log(level, message, extra={"fields": _redact(clean)})


class JsonFormatter(logging.Formatter):
    """One JSON object per line, for shipping into a log aggregator."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname.lower(),
            "message": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class ConsoleFormatter(logging.Formatter):
    """Human-readable lines, for running the bridge in a terminal."""

    COLOURS: ClassVar[dict[str, str]] = {
        "DEBUG": "\033[90m",
        "INFO": "\033[32m",
        "WARNING": "\033[33m",
        "ERROR": "\033[31m",
        "CRITICAL": "\033[35m",
    }
    RESET = "\033[0m"

    def __init__(self, colour: bool = True) -> None:
        super().__init__()
        self.colour = colour

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        level = record.levelname
        if self.colour:
            level = f"{self.COLOURS.get(record.levelname, '')}{level}{self.RESET}"

        line = f"[{timestamp}] {level}: {record.getMessage()}"

        fields = getattr(record, "fields", None)
        if isinstance(fields, dict) and fields:
            rendered = " ".join(f"{key}={_render(value)}" for key, value in fields.items())
            line = f"{line}  {rendered}"
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return line


def _render(value: Any) -> str:
    if isinstance(value, str):
        return value if value.isprintable() and " " not in value else json.dumps(value)
    if isinstance(value, dict | list):
        return json.dumps(value, default=str)
    return str(value)


def _to_python_level(level: str) -> int:
    mapping = {
        "trace": logging.DEBUG,
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warn": logging.WARNING,
        "warning": logging.WARNING,
        "error": logging.ERROR,
        "fatal": logging.CRITICAL,
        "silent": logging.CRITICAL + 10,
    }
    return mapping.get(level.lower(), logging.INFO)


def configure_logging(level: str = "info", pretty: bool = False) -> BridgeLogger:
    """Install the root handler and return the bridge's logger."""
    root = logging.getLogger("mqtt_atmowx_bridge")
    root.handlers.clear()
    root.setLevel(_to_python_level(level))
    root.propagate = False

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        ConsoleFormatter(colour=sys.stderr.isatty()) if pretty else JsonFormatter()
    )
    root.addHandler(handler)

    return BridgeLogger(root)


def null_logger() -> BridgeLogger:
    """A logger that discards everything, for tests and library use."""
    logger = logging.getLogger("mqtt_atmowx_bridge.null")
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    logger.setLevel(logging.CRITICAL + 10)
    return BridgeLogger(logger)
