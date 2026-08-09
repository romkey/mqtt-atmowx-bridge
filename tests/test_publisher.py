"""Publishing: record keys, the retry queue, and what gets dropped."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mqtt_atmowx_bridge.atproto.client import AtpClient
from mqtt_atmowx_bridge.atproto.session import SessionManager
from mqtt_atmowx_bridge.atproto.session_store import MemorySessionStore
from mqtt_atmowx_bridge.atproto.tid import tid_from_datetime
from mqtt_atmowx_bridge.bridge.publisher import ObservationPublisher
from mqtt_atmowx_bridge.logging_setup import null_logger

from .helpers import FakePds, error_response, json_response, session_response

CREATE = "com.atproto.server.createSession"
PUT = "com.atproto.repo.putRecord"

WHEN = datetime(2026, 8, 7, 21, 35, 0, tzinfo=UTC)
RECORD = {
    "$type": "net.atmowx.observation",
    "station": "at://did:plc:test/net.atmowx.station/abc",
    "observedAt": "2026-08-07T21:35:00.000Z",
    "temperature": {"value": 215, "scale": -1},
}


@pytest.fixture
def pds() -> FakePds:
    return FakePds().on(CREATE, json_response(session_response()))


def publisher_for(pds: FakePds, **kwargs: object) -> ObservationPublisher:
    session = SessionManager(
        service="https://bsky.social",
        identifier="me.example.com",
        password="abcd-efgh-ijkl-mnop",
        store=MemorySessionStore(),
        client=pds.client(),
    )
    client = AtpClient(session, client=pds.client(), max_retries=0, base_backoff_seconds=0.001)
    return ObservationPublisher(
        client=client,
        logger=null_logger(),
        **kwargs,  # type: ignore[arg-type]
    )


class TestRecordKeys:
    def test_derives_the_key_from_the_observation_time(self, pds: FakePds) -> None:
        pds.on(PUT, json_response({"uri": "at://x"}))

        publisher_for(pds).publish(RECORD, WHEN)

        assert pds.calls_to(PUT)[0].body["rkey"] == tid_from_datetime(WHEN)

    def test_republishing_the_same_moment_overwrites_rather_than_duplicates(
        self, pds: FakePds
    ) -> None:
        pds.on(PUT, json_response({"uri": "at://x"}))
        publisher = publisher_for(pds)

        publisher.publish(RECORD, WHEN)
        publisher.publish({**RECORD, "temperature": {"value": 220, "scale": -1}}, WHEN)

        first, second = pds.calls_to(PUT)
        assert first.body["rkey"] == second.body["rkey"]

    def test_different_moments_get_different_keys(self, pds: FakePds) -> None:
        pds.on(PUT, json_response({"uri": "at://x"}))
        publisher = publisher_for(pds)

        publisher.publish(RECORD, WHEN)
        publisher.publish(RECORD, WHEN + timedelta(minutes=5))

        first, second = pds.calls_to(PUT)
        assert first.body["rkey"] != second.body["rkey"]
        assert first.body["rkey"] < second.body["rkey"]


class TestSuccess:
    def test_reports_a_successful_publish(self, pds: FakePds) -> None:
        pds.on(PUT, json_response({"uri": "at://x", "cid": "bafy"}))
        publisher = publisher_for(pds)

        assert publisher.publish(RECORD, WHEN) is True
        assert publisher.status()["published"] == 1
        assert publisher.status()["last_uri"] == "at://x"

    def test_sends_the_record_unchanged(self, pds: FakePds) -> None:
        pds.on(PUT, json_response({"uri": "at://x"}))

        publisher_for(pds).publish(RECORD, WHEN)

        assert pds.calls_to(PUT)[0].body["record"] == RECORD
        assert pds.calls_to(PUT)[0].body["collection"] == "net.atmowx.observation"


class TestRetryQueue:
    def test_queues_a_record_the_server_could_not_take(self, pds: FakePds) -> None:
        pds.on(PUT, error_response("InternalServerError", status=500))
        publisher = publisher_for(pds, queue_size=10)

        assert publisher.publish(RECORD, WHEN) is False
        assert publisher.queue_length == 1

    def test_replays_the_queue_after_the_next_success(self, pds: FakePds) -> None:
        pds.on(
            PUT,
            error_response("InternalServerError", status=500),
            json_response({"uri": "at://second"}),
            json_response({"uri": "at://first-retried"}),
        )
        publisher = publisher_for(pds, queue_size=10)

        publisher.publish(RECORD, WHEN)
        publisher.publish(RECORD, WHEN + timedelta(minutes=5))

        assert publisher.queue_length == 0
        assert publisher.status()["published"] == 2

    def test_replays_in_the_order_the_observations_were_taken(self, pds: FakePds) -> None:
        pds.on(
            PUT,
            error_response("InternalServerError", status=500),
            error_response("InternalServerError", status=500),
            json_response({"uri": "at://x"}),
        )
        publisher = publisher_for(pds, queue_size=10)

        publisher.publish(RECORD, WHEN)
        publisher.publish(RECORD, WHEN + timedelta(minutes=5))
        publisher.flush()

        replayed = [call.body["rkey"] for call in pds.calls_to(PUT)[2:]]
        assert replayed == sorted(replayed)

    def test_stops_replaying_at_the_first_record_still_failing(self, pds: FakePds) -> None:
        pds.on(PUT, error_response("InternalServerError", status=500))
        publisher = publisher_for(pds, queue_size=10)

        publisher.publish(RECORD, WHEN)
        publisher.publish(RECORD, WHEN + timedelta(minutes=5))
        before = len(pds.calls_to(PUT))
        publisher.flush()

        # One attempt per flush, not a stampede through the whole queue.
        assert len(pds.calls_to(PUT)) == before + 1
        assert publisher.queue_length == 2

    def test_drops_the_oldest_when_the_queue_is_full(self, pds: FakePds) -> None:
        pds.on(PUT, error_response("InternalServerError", status=500))
        publisher = publisher_for(pds, queue_size=2)

        for minutes in (0, 5, 10):
            publisher.publish(RECORD, WHEN + timedelta(minutes=minutes))

        assert publisher.queue_length == 2
        assert publisher.status()["dropped_from_queue"] == 1

    def test_does_not_queue_when_the_queue_is_disabled(self, pds: FakePds) -> None:
        pds.on(PUT, error_response("InternalServerError", status=500))
        publisher = publisher_for(pds, queue_size=0)

        publisher.publish(RECORD, WHEN)

        assert publisher.queue_length == 0


class TestPermanentFailures:
    def test_drops_a_record_the_server_will_never_accept(self, pds: FakePds) -> None:
        pds.on(PUT, error_response("InvalidRequest", "Invalid record", 400))
        publisher = publisher_for(pds, queue_size=10)

        assert publisher.publish(RECORD, WHEN) is False
        # Retrying a malformed record forever would block everything behind it.
        assert publisher.queue_length == 0
        assert publisher.status()["dropped_from_queue"] == 1

    def test_keeps_a_rate_limited_record(self, pds: FakePds) -> None:
        pds.on(PUT, error_response("RateLimitExceeded", status=429))
        publisher = publisher_for(pds, queue_size=10)

        publisher.publish(RECORD, WHEN)

        assert publisher.queue_length == 1

    def test_records_the_last_error(self, pds: FakePds) -> None:
        pds.on(PUT, error_response("InternalServerError", "boom", 500))
        publisher = publisher_for(pds, queue_size=10)

        publisher.publish(RECORD, WHEN)

        assert "boom" in str(publisher.status()["last_error"])


class TestDryRun:
    def test_builds_the_record_without_writing_it(self, pds: FakePds) -> None:
        publisher = publisher_for(pds, dry_run=True)

        assert publisher.publish(RECORD, WHEN) is True
        assert pds.calls_to(PUT) == []
