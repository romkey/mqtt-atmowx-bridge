"""A small HTTP endpoint for liveness probes and eyeballing the bridge's state.

``/health`` answers 200 while the bridge is alive and connected to MQTT and 503
otherwise, which is what a container orchestrator wants. ``/status`` returns the
full picture as JSON for humans.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .logging_setup import BridgeLogger

StatusProvider = Callable[[], dict[str, Any]]


class HealthServer:
    """Serves ``/health`` and ``/status`` on a background thread."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        status: StatusProvider,
        logger: BridgeLogger,
    ) -> None:
        self._host = host
        self._port = port
        self._status = status
        self._log = logger
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        """The bound port, which differs from the configured one when it was 0."""
        return self._server.server_address[1] if self._server else self._port

    def start(self) -> None:
        provider = self._status
        logger = self._log

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                path = self.path.split("?", 1)[0].rstrip("/") or "/"

                if path in {"/health", "/healthz", "/"}:
                    snapshot = provider()
                    mqtt = snapshot.get("mqtt", {})
                    healthy = bool(mqtt.get("connected"))
                    self._respond(
                        200 if healthy else 503,
                        {
                            "status": "ok" if healthy else "degraded",
                            "mqtt_connected": mqtt.get("connected", False),
                            "uptime_seconds": snapshot.get("uptime_seconds"),
                        },
                    )
                elif path == "/status":
                    self._respond(200, provider())
                else:
                    self._respond(404, {"error": "not found"})

            def _respond(self, status: int, body: dict[str, Any]) -> None:
                payload = json.dumps(body, indent=2, default=str).encode("utf-8")
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: Any) -> None:
                # Route access logs through our logger instead of stderr.
                logger.debug("health request", request=format % args)

        self._server = ThreadingHTTPServer((self._host, self._port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="health", daemon=True
        )
        self._thread.start()
        self._log.info("health endpoint listening", host=self._host, port=self.port)

    def stop(self) -> None:
        server = self._server
        if server is None:
            return
        self._server = None
        server.shutdown()
        server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
