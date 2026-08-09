"""A minimal MQTT 3.1.1 broker, just enough to test the bridge against real paho.

Supports CONNECT, SUBSCRIBE, PUBLISH at QoS 0 (both directions), PINGREQ and
DISCONNECT — the entire set of packets the bridge exchanges. Anything beyond
that is out of scope; this exists so the end-to-end test drives the actual MQTT
client rather than a stub of it.
"""

from __future__ import annotations

import contextlib
import socket
import threading
from dataclasses import dataclass, field

CONNECT = 1
CONNACK = 2
PUBLISH = 3
SUBSCRIBE = 8
SUBACK = 9
UNSUBSCRIBE = 10
UNSUBACK = 11
PINGREQ = 12
PINGRESP = 13
DISCONNECT = 14


def encode_remaining_length(length: int) -> bytes:
    out = bytearray()
    while True:
        byte = length % 128
        length //= 128
        if length > 0:
            byte |= 0x80
        out.append(byte)
        if length == 0:
            return bytes(out)


def encode_string(value: str) -> bytes:
    raw = value.encode("utf-8")
    return len(raw).to_bytes(2, "big") + raw


@dataclass
class _Client:
    connection: socket.socket
    subscriptions: list[str] = field(default_factory=list)


class MqttTestBroker:
    """An in-process broker on an ephemeral port."""

    def __init__(self, host: str = "127.0.0.1") -> None:
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind((host, 0))
        self._listener.listen(8)
        self._clients: list[_Client] = []
        self._lock = threading.Lock()
        self._running = True
        self._subscribed = threading.Event()
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    @property
    def port(self) -> int:
        return int(self._listener.getsockname()[1])

    @property
    def url(self) -> str:
        return f"mqtt://127.0.0.1:{self.port}"

    def wait_for_subscription(self, timeout: float = 5.0) -> bool:
        return self._subscribed.wait(timeout)

    def publish(self, topic: str, payload: str | bytes, retain: bool = False) -> None:
        """Deliver a message to every client subscribed to a matching filter."""
        from mqtt_atmowx_bridge.mqtt.topic import topic_matches

        data = payload.encode("utf-8") if isinstance(payload, str) else payload
        body = encode_string(topic) + data
        packet = (
            bytes([(PUBLISH << 4) | (0x01 if retain else 0x00)])
            + encode_remaining_length(len(body))
            + body
        )

        with self._lock:
            clients = list(self._clients)
        for client in clients:
            if any(topic_matches(f, topic) for f in client.subscriptions):
                with contextlib.suppress(OSError):
                    client.connection.sendall(packet)

    def close(self) -> None:
        self._running = False
        with self._lock:
            clients = list(self._clients)
            self._clients.clear()
        for client in clients:
            with contextlib.suppress(OSError):
                client.connection.close()
        with contextlib.suppress(OSError):
            self._listener.close()
        self._thread.join(timeout=2)

    def __enter__(self) -> MqttTestBroker:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # -- internals ----------------------------------------------------------

    def _accept_loop(self) -> None:
        while self._running:
            try:
                connection, _ = self._listener.accept()
            except OSError:
                return
            client = _Client(connection=connection)
            with self._lock:
                self._clients.append(client)
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client: _Client) -> None:
        connection = client.connection
        try:
            while self._running:
                header = self._read_exactly(connection, 1)
                if header is None:
                    return
                packet_type = header[0] >> 4

                length = self._read_remaining_length(connection)
                if length is None:
                    return
                body = self._read_exactly(connection, length) if length else b""
                if body is None:
                    return

                if packet_type == CONNECT:
                    connection.sendall(bytes([CONNACK << 4, 0x02, 0x00, 0x00]))
                elif packet_type == SUBSCRIBE:
                    self._handle_subscribe(client, body)
                elif packet_type == UNSUBSCRIBE:
                    packet_id = body[0:2]
                    connection.sendall(bytes([UNSUBACK << 4, 0x02]) + packet_id)
                elif packet_type == PUBLISH:
                    self._handle_publish(header[0], body)
                elif packet_type == PINGREQ:
                    connection.sendall(bytes([PINGRESP << 4, 0x00]))
                elif packet_type == DISCONNECT:
                    return
        except OSError:
            return
        finally:
            with self._lock:
                if client in self._clients:
                    self._clients.remove(client)
            with contextlib.suppress(OSError):
                connection.close()

    def _handle_subscribe(self, client: _Client, body: bytes) -> None:
        packet_id = body[0:2]
        offset = 2
        granted = bytearray()

        while offset < len(body):
            length = int.from_bytes(body[offset : offset + 2], "big")
            offset += 2
            topic_filter = body[offset : offset + length].decode("utf-8")
            offset += length
            requested_qos = body[offset]
            offset += 1

            client.subscriptions.append(topic_filter)
            granted.append(min(requested_qos, 0))  # we only ever grant QoS 0

        payload = packet_id + bytes(granted)
        client.connection.sendall(
            bytes([SUBACK << 4]) + encode_remaining_length(len(payload)) + payload
        )
        self._subscribed.set()

    def _handle_publish(self, first_byte: int, body: bytes) -> None:
        qos = (first_byte >> 1) & 0x03
        retain = bool(first_byte & 0x01)

        length = int.from_bytes(body[0:2], "big")
        topic = body[2 : 2 + length].decode("utf-8")
        offset = 2 + length
        if qos > 0:
            offset += 2  # skip the packet identifier
        self.publish(topic, body[offset:], retain=retain)

    @staticmethod
    def _read_exactly(connection: socket.socket, count: int) -> bytes | None:
        chunks = bytearray()
        while len(chunks) < count:
            try:
                chunk = connection.recv(count - len(chunks))
            except OSError:
                return None
            if not chunk:
                return None
            chunks.extend(chunk)
        return bytes(chunks)

    @classmethod
    def _read_remaining_length(cls, connection: socket.socket) -> int | None:
        multiplier = 1
        value = 0
        for _ in range(4):
            byte = cls._read_exactly(connection, 1)
            if byte is None:
                return None
            value += (byte[0] & 0x7F) * multiplier
            if not byte[0] & 0x80:
                return value
            multiplier *= 128
        return None
