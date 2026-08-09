"""The MQTT side of the bridge: connect, stay connected, and hand decoded
messages to a callback.

Subscriptions are (re)issued on every ``on_connect`` rather than once at
startup. paho does not resubscribe for us, so a broker restart or a session that
was not preserved would otherwise leave us connected but subscribed to nothing —
a silent failure that looks exactly like quiet weather.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from ..config import Config
from ..logging_setup import BridgeLogger

DEFAULT_PORTS = {"mqtt": 1883, "tcp": 1883, "mqtts": 8883, "ssl": 8883, "ws": 80, "wss": 443}
TLS_SCHEMES = {"mqtts", "ssl", "wss"}
WEBSOCKET_SCHEMES = {"ws", "wss"}


@dataclass(slots=True)
class IncomingMessage:
    topic: str
    payload: bytes
    retain: bool
    received_at: datetime


MessageHandler = Callable[[IncomingMessage], None]


class MqttBridgeClient:
    """A paho client wired up for the bridge's needs."""

    def __init__(self, config: Config, logger: BridgeLogger, on_message: MessageHandler) -> None:
        self._config = config
        self._log = logger
        self._on_message = on_message
        self._client: mqtt.Client | None = None
        self._connected = threading.Event()
        self._messages_received = 0
        self._last_message_at: datetime | None = None

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def status(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "url": self._config.mqtt.url,
            "topics": list(self._config.topics),
            "messages_received": self._messages_received,
            "last_message_at": (
                self._last_message_at.isoformat() if self._last_message_at else None
            ),
        }

    def start(self) -> None:
        settings = self._config.mqtt
        parsed = urlparse(settings.url)
        scheme = parsed.scheme
        host = parsed.hostname or "localhost"
        port = parsed.port or DEFAULT_PORTS.get(scheme, 1883)

        client = mqtt.Client(
            CallbackAPIVersion.VERSION2,
            client_id=settings.client_id or "",
            clean_session=settings.clean if settings.protocol_version == 4 else None,
            protocol=mqtt.MQTTv5 if settings.protocol_version == 5 else mqtt.MQTTv311,
            transport="websockets" if scheme in WEBSOCKET_SCHEMES else "tcp",
        )

        if settings.username:
            client.username_pw_set(settings.username, settings.password or None)

        if scheme in TLS_SCHEMES or settings.tls is not None:
            tls = settings.tls
            client.tls_set(
                ca_certs=tls.ca if tls else None,
                certfile=tls.cert if tls else None,
                keyfile=tls.key if tls else None,
            )
            if tls is not None and not tls.reject_unauthorized:
                self._log.warn("TLS certificate verification is disabled for the broker connection")
                client.tls_insecure_set(True)

        client.on_connect = self._handle_connect
        client.on_disconnect = self._handle_disconnect
        client.on_message = self._handle_message
        client.reconnect_delay_set(
            min_delay=max(1, int(settings.reconnect_period_seconds)), max_delay=120
        )

        self._client = client
        # connect_async + loop_start means a broker that is not up yet is
        # retried rather than crashing the bridge on boot.
        client.connect_async(host, port, keepalive=settings.keepalive_seconds)
        client.loop_start()

    def wait_until_connected(self, timeout: float) -> bool:
        return self._connected.wait(timeout)

    def stop(self) -> None:
        client = self._client
        if client is None:
            return
        self._client = None
        try:
            client.disconnect()
        finally:
            client.loop_stop()
        self._connected.clear()

    # -- paho callbacks (called on paho's network thread) -------------------

    def _handle_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: Any,
        reason_code: Any,
        properties: Any = None,
    ) -> None:
        if getattr(reason_code, "is_failure", False):
            self._log.error("MQTT connection refused", reason=str(reason_code))
            return

        self._connected.set()
        self._log.info("connected to the MQTT broker", url=self._config.mqtt.url)

        if self._config.topics:
            # One SUBSCRIBE with every filter. Issuing them one at a time leaves a
            # window where a message on a later topic arrives unsubscribed.
            filters: list[tuple[str, int]] = [
                (topic, self._config.mqtt.qos) for topic in self._config.topics
            ]
            result, _ = client.subscribe(filters)
            if result == mqtt.MQTT_ERR_SUCCESS:
                for topic in self._config.topics:
                    self._log.info("subscribed", topic=topic, qos=self._config.mqtt.qos)
            else:
                self._log.error(
                    "subscribe failed",
                    topics=list(self._config.topics),
                    error=mqtt.error_string(result),
                )

    def _handle_disconnect(
        self,
        client: mqtt.Client,
        userdata: Any,
        disconnect_flags: Any,
        reason_code: Any = None,
        properties: Any = None,
    ) -> None:
        was_connected = self._connected.is_set()
        self._connected.clear()
        if was_connected:
            self._log.warn("MQTT connection closed", reason=str(reason_code))

    def _handle_message(self, client: mqtt.Client, userdata: Any, message: Any) -> None:
        self._messages_received += 1
        received_at = datetime.now(UTC)
        self._last_message_at = received_at
        try:
            self._on_message(
                IncomingMessage(
                    topic=message.topic,
                    payload=bytes(message.payload),
                    retain=bool(message.retain),
                    received_at=received_at,
                )
            )
        except Exception as error:  # noqa: BLE001 - never let paho's thread die
            self._log.error("failed to handle a message", topic=message.topic, err=str(error))
