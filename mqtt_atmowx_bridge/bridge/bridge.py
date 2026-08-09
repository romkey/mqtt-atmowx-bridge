"""The bridge itself: MQTT in, net.atmowx.observation out.

Two threads do the work. paho's network thread feeds the collector; a publisher
thread wakes on a schedule (or on a trigger message), takes a snapshot of the
current readings, fills in any derived fields, and writes a record. Keeping the
publish off paho's thread matters — an unreachable PDS must not stall message
handling.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from typing import Any

from ..atproto.client import AtpClient
from ..config import Config, DeriveMode
from ..logging_setup import BridgeLogger
from ..mqtt.client import IncomingMessage, MqttBridgeClient
from ..observation.derive import (
    dew_point_celsius,
    sea_level_pressure_hpa,
    station_pressure_hpa,
)
from ..observation.fields import CoreTarget, field_spec
from ..observation.record import Reading, build_observation
from .collector import ReadingCollector
from .publisher import ObservationPublisher


class Bridge:
    """Wires the collector, the derivations and the publisher together."""

    def __init__(
        self,
        *,
        config: Config,
        client: AtpClient,
        logger: BridgeLogger,
    ) -> None:
        self._config = config
        self._log = logger
        self._collector = ReadingCollector(config, logger.bind("collector"))
        self._publisher = ObservationPublisher(
            client=client,
            logger=logger.bind("publisher"),
            queue_size=config.publish.queue_size,
            dry_run=config.publish.dry_run,
        )
        self._mqtt = MqttBridgeClient(config, logger.bind("mqtt"), self._handle_message)

        self._stopping = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._pending_trigger = False
        self._last_published_at = 0.0
        self._last_signature: str | None = None
        self._started_at = datetime.now(UTC)
        self._observations_built = 0
        self._skipped = 0

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        self._log.info(
            "starting the bridge",
            station=self._config.station,
            mode=self._config.publish.mode,
            topics=list(self._config.topics),
            dry_run=self._config.publish.dry_run,
        )
        self._mqtt.start()
        self._thread = threading.Thread(target=self._run, name="publisher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._log.info("stopping the bridge")
        self._stopping.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=10)
        self._mqtt.stop()

    def wait(self) -> None:
        """Block until :meth:`stop` is called."""
        while not self._stopping.wait(3600):
            pass

    def status(self) -> dict[str, Any]:
        return {
            "station": self._config.station,
            "started_at": self._started_at.isoformat(),
            "uptime_seconds": round((datetime.now(UTC) - self._started_at).total_seconds()),
            "observations_built": self._observations_built,
            "skipped": self._skipped,
            "mqtt": self._mqtt.status(),
            "collector": self._collector.status(),
            "publisher": self._publisher.status(),
        }

    # -- MQTT ---------------------------------------------------------------

    def _handle_message(self, message: IncomingMessage) -> None:
        result = self._collector.ingest(message)
        if result.triggered and self._config.publish.mode == "onMessage":
            self._pending_trigger = True
            self._wake.set()

    # -- publish loop -------------------------------------------------------

    def _run(self) -> None:
        try:
            if self._config.publish.mode == "interval":
                self._run_interval()
            else:
                self._run_on_message()
        except Exception:  # noqa: BLE001 - a dead publisher thread is worse
            self._log.exception("the publisher thread stopped unexpectedly")

    def _run_interval(self) -> None:
        interval = self._config.publish.interval_seconds
        while not self._stopping.is_set():
            self._wake.wait(self._seconds_until_next(interval))
            self._wake.clear()
            if self._stopping.is_set():
                return
            self._publish_once("interval")

    def _run_on_message(self) -> None:
        minimum = self._config.publish.min_interval_seconds
        while not self._stopping.is_set():
            if self._pending_trigger:
                waited = time.monotonic() - self._last_published_at
                if waited >= minimum:
                    self._pending_trigger = False
                    self._publish_once("trigger")
                    continue
                timeout: float | None = minimum - waited
            else:
                timeout = None

            self._wake.wait(timeout)
            self._wake.clear()

    def _seconds_until_next(self, interval: int) -> float:
        now = time.time()
        if not self._config.publish.align_to_interval:
            return interval
        # Land on interval boundaries so records line up at :00, :05, :10.
        return interval - (now % interval)

    def _publish_once(self, reason: str) -> None:
        try:
            self._build_and_publish(reason)
        except Exception as error:  # noqa: BLE001 - keep the loop alive
            self._log.error("failed to publish an observation", reason=reason, err=str(error))

    def _build_and_publish(self, reason: str) -> None:
        publish = self._config.publish
        now = datetime.now(UTC)
        readings = self._collector.snapshot(now, publish.max_reading_age_seconds)

        if not readings:
            self._log.debug("nothing to publish: no current readings", reason=reason)
            return

        self._apply_derivations(readings, now)

        if len(readings) < publish.min_fields:
            self._skipped += 1
            self._log.debug(
                "not publishing: too few measurements",
                have=len(readings),
                want=publish.min_fields,
            )
            return

        observed_at = self._observed_at_for(readings, now)
        result = build_observation(
            station=self._config.station, observed_at=observed_at, readings=readings
        )

        for rejection in result.rejected:
            self._log.warn(
                "dropping an implausible reading",
                field=rejection["name"],
                value=rejection["value"],
                reason=rejection["reason"],
            )

        if len(result.included) < publish.min_fields:
            self._skipped += 1
            self._log.debug(
                "not publishing: too few measurements survived validation",
                have=len(result.included),
                want=publish.min_fields,
            )
            return

        if publish.skip_unchanged:
            # Compare everything but observedAt: identical readings at a new
            # time are a duplicate, not an update.
            signature = repr(
                {key: value for key, value in result.record.items() if key != "observedAt"}
            )
            if signature == self._last_signature:
                self._skipped += 1
                self._log.debug("not publishing: measurements are unchanged")
                return
            self._last_signature = signature

        self._observations_built += 1
        self._last_published_at = time.monotonic()
        self._publisher.publish(result.record, observed_at)

    def _observed_at_for(self, readings: dict[str, Reading], now: datetime) -> datetime:
        publish = self._config.publish
        if publish.timestamp_source == "received":
            moment = now
        else:
            moment = max((reading.observed_at for reading in readings.values()), default=now)

        if publish.round_to_seconds > 1:
            step = publish.round_to_seconds
            rounded = int(moment.timestamp() // step) * step
            return datetime.fromtimestamp(rounded, tz=UTC)
        return moment

    # -- derived values -----------------------------------------------------

    def _apply_derivations(self, readings: dict[str, Reading], now: datetime) -> None:
        derive = self._config.derive

        def value_of(name: str) -> float | None:
            reading = readings.get(name)
            return reading.value if reading else None

        def should(mode: DeriveMode, name: str) -> bool:
            if mode == "never":
                return False
            return mode == "always" or name not in readings

        def put(name: str, value: float, sources: list[str]) -> None:
            spec = field_spec(name)
            newest = max(
                (readings[source].observed_at for source in sources if source in readings),
                default=now,
            )
            readings[name] = Reading(
                target=CoreTarget(field=name),
                value=value,
                dimension=spec.dimension,
                decimals=spec.decimals,
                observed_at=newest,
                received_at=now,
                source=f"derived from {' and '.join(sources)}",
            )

        temperature = value_of("temperature")

        if should(derive.dew_point, "dewPoint"):
            humidity = value_of("relativeHumidity")
            if temperature is not None and humidity is not None:
                put(
                    "dewPoint",
                    dew_point_celsius(temperature, humidity),
                    ["temperature", "relativeHumidity"],
                )

        elevation = derive.elevation_meters
        if should(derive.pressure_sea_level, "pressureSeaLevel") and elevation is not None:
            station = value_of("pressureStation")
            if station is not None and temperature is not None:
                put(
                    "pressureSeaLevel",
                    sea_level_pressure_hpa(station, elevation, temperature),
                    ["pressureStation", "temperature"],
                )

        if should(derive.pressure_station, "pressureStation") and elevation is not None:
            sea_level = value_of("pressureSeaLevel")
            if sea_level is not None and temperature is not None:
                put(
                    "pressureStation",
                    station_pressure_hpa(sea_level, elevation, temperature),
                    ["pressureSeaLevel", "temperature"],
                )

    # -- test seams ---------------------------------------------------------

    @property
    def collector(self) -> ReadingCollector:
        return self._collector

    @property
    def publisher(self) -> ObservationPublisher:
        return self._publisher

    def publish_now(self, reason: str = "manual") -> None:
        """Build and publish immediately, ignoring the schedule."""
        self._publish_once(reason)

    def wait_until_connected(self, timeout: float = 30.0) -> bool:
        return self._mqtt.wait_until_connected(timeout)
