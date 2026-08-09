"""Writing observations to the repo.

Record keys are TIDs derived from ``observedAt``, which makes a write
idempotent: if we are unsure whether a record landed, publishing it again
overwrites the same key instead of creating a second reading for the same
moment. That is also what makes the retry queue safe — a queued record can be
replayed minutes later without corrupting the series.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..atproto.client import AtpClient
from ..atproto.errors import NetworkError, XrpcError
from ..atproto.tid import tid_from_datetime
from ..logging_setup import BridgeLogger
from ..observation.record import OBSERVATION_NSID


@dataclass(slots=True)
class PendingRecord:
    rkey: str
    record: dict[str, Any]
    queued_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    attempts: int = 0


#: ``retry`` stays in the queue; ``dropped`` never will be accepted.
WRITTEN = "written"
RETRY = "retry"
DROPPED = "dropped"


class ObservationPublisher:
    """Publishes records, holding on to the ones the server could not take."""

    def __init__(
        self,
        *,
        client: AtpClient,
        logger: BridgeLogger,
        queue_size: int = 100,
        dry_run: bool = False,
    ) -> None:
        self._client = client
        self._log = logger
        self._queue_size = queue_size
        self._dry_run = dry_run

        self._queue: deque[PendingRecord] = deque()
        self._published = 0
        self._failed = 0
        self._dropped = 0
        self._last_published_at: datetime | None = None
        self._last_uri: str | None = None
        self._last_error: str | None = None

    @property
    def queue_length(self) -> int:
        return len(self._queue)

    def status(self) -> dict[str, Any]:
        return {
            "published": self._published,
            "failed": self._failed,
            "dropped_from_queue": self._dropped,
            "queued": len(self._queue),
            "last_published_at": (
                self._last_published_at.isoformat() if self._last_published_at else None
            ),
            "last_uri": self._last_uri,
            "last_error": self._last_error,
        }

    def publish(self, record: dict[str, Any], observed_at: datetime) -> bool:
        """Publish one observation.

        Failures that are worth another try are queued; failures that are not (a
        malformed record, a takedown) are logged and dropped, because retrying
        them forever would wedge the queue.
        """
        rkey = tid_from_datetime(observed_at)

        if self._dry_run:
            self._log.info(
                "dry run: built an observation but did not publish it",
                rkey=rkey,
                record=record,
            )
            self._last_published_at = datetime.now(UTC)
            return True

        pending = PendingRecord(rkey=rkey, record=record)
        outcome = self._write(pending)
        if outcome == RETRY:
            self._enqueue(pending)

        # A successful write is a good moment to drain anything held back earlier.
        if outcome == WRITTEN and self._queue:
            self.flush()
        return outcome == WRITTEN

    def flush(self) -> None:
        """Retry queued records, oldest first. Stops at the first one still failing."""
        while self._queue:
            pending = self._queue[0]
            if self._write(pending) == RETRY:
                return
            # Written or permanently rejected: either way it leaves the queue.
            self._queue.popleft()

    def _write(self, pending: PendingRecord) -> str:
        pending.attempts += 1
        try:
            result = self._client.put_record(
                collection=OBSERVATION_NSID, rkey=pending.rkey, record=pending.record
            )
        except XrpcError as error:
            self._failed += 1
            self._last_error = str(error)
            if not error.is_retryable and not (error.is_expired_token or error.is_invalid_token):
                # 400 InvalidRequest, a lexicon the PDS rejects, a takedown:
                # replaying this record will fail identically every time.
                self._drop(pending, error)
                return DROPPED
            self._log.warn(
                "could not publish; will retry",
                rkey=pending.rkey,
                attempts=pending.attempts,
                err=str(error),
            )
            return RETRY
        except (NetworkError, OSError) as error:
            self._failed += 1
            self._last_error = str(error)
            self._log.warn(
                "could not publish; will retry",
                rkey=pending.rkey,
                attempts=pending.attempts,
                err=str(error),
            )
            return RETRY

        self._published += 1
        self._last_published_at = datetime.now(UTC)
        self._last_uri = str(result.get("uri", ""))
        self._last_error = None
        self._log.info(
            "published an observation",
            uri=self._last_uri,
            observed_at=pending.record.get("observedAt"),
            attempts=pending.attempts,
        )
        return WRITTEN

    def _drop(self, pending: PendingRecord, error: Exception) -> None:
        self._dropped += 1
        self._log.error(
            "dropping an observation the server will never accept",
            rkey=pending.rkey,
            observed_at=pending.record.get("observedAt"),
            err=str(error),
        )

    def _enqueue(self, pending: PendingRecord) -> None:
        if self._queue_size == 0:
            return

        self._queue.append(pending)
        while len(self._queue) > self._queue_size:
            evicted = self._queue.popleft()
            self._dropped += 1
            self._log.warn(
                "the publish queue is full; dropping the oldest observation",
                rkey=evicted.rkey,
                queue_size=self._queue_size,
            )
