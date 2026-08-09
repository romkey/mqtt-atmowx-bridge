"""XRPC client: authentication, token-rejection recovery, retries."""

from __future__ import annotations

import httpx
import pytest

from mqtt_atmowx_bridge.atproto.client import AtpClient
from mqtt_atmowx_bridge.atproto.errors import NetworkError, XrpcError, parse_retry_after
from mqtt_atmowx_bridge.atproto.jwt import decode_jwt
from mqtt_atmowx_bridge.atproto.session import SessionManager
from mqtt_atmowx_bridge.atproto.session_store import MemorySessionStore

from .helpers import FakePds, error_response, json_response, session_response

CREATE = "com.atproto.server.createSession"
REFRESH = "com.atproto.server.refreshSession"
PUT = "com.atproto.repo.putRecord"
GET = "com.atproto.repo.getRecord"
LIST = "com.atproto.repo.listRecords"

OBSERVATION = {"$type": "net.atmowx.observation", "temperature": {"value": 215, "scale": -1}}


def client_for(pds: FakePds, **kwargs: object) -> AtpClient:
    session = SessionManager(
        service="https://bsky.social",
        identifier="station.example.com",
        password="abcd-efgh-ijkl-mnop",
        store=MemorySessionStore(),
        client=pds.client(),
    )
    return AtpClient(
        session,
        client=pds.client(),
        base_backoff_seconds=0.001,
        max_backoff_seconds=0.01,
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.fixture
def pds() -> FakePds:
    return FakePds().on(CREATE, json_response(session_response()))


class TestAuthentication:
    def test_sends_the_access_token_not_the_refresh_token(self, pds: FakePds) -> None:
        pds.on(PUT, json_response({"uri": "at://did:plc:test/net.atmowx.observation/abc"}))
        subject = client_for(pds)

        subject.put_record(collection="net.atmowx.observation", rkey="abc", record=OBSERVATION)

        session = subject.session.session()
        assert pds.calls_to(PUT)[0].bearer == session.access_jwt
        assert decode_jwt(session.access_jwt)["scope"] == "com.atproto.access"

    def test_writes_to_the_pds_from_the_did_document(self, pds: FakePds) -> None:
        pds.on(PUT, json_response({"uri": "at://did:plc:test/net.atmowx.observation/abc"}))

        client_for(pds).put_record(
            collection="net.atmowx.observation", rkey="abc", record=OBSERVATION
        )

        assert pds.calls_to(PUT)[0].url.startswith("https://pds.example.com/")

    def test_puts_the_record_at_the_key_it_was_given(self, pds: FakePds) -> None:
        pds.on(PUT, json_response({"uri": "at://x", "cid": "bafy"}))

        client_for(pds).put_record(
            collection="net.atmowx.observation", rkey="3mremies2t222", record=OBSERVATION
        )

        assert pds.calls_to(PUT)[0].body == {
            "repo": "did:plc:test",
            "collection": "net.atmowx.observation",
            "rkey": "3mremies2t222",
            "record": OBSERVATION,
        }

    def test_omits_validate_so_an_unfamiliar_lexicon_is_not_rejected(self, pds: FakePds) -> None:
        pds.on(PUT, json_response({"uri": "at://x"}))

        client_for(pds).put_record(
            collection="net.atmowx.observation", rkey="abc", record=OBSERVATION
        )

        assert "validate" not in pds.calls_to(PUT)[0].body


class TestTokenRecovery:
    def test_renews_and_retries_once_when_the_token_is_expired(self, pds: FakePds) -> None:
        pds.on(REFRESH, json_response(session_response(access_jti="renewed")))
        pds.on(
            PUT,
            error_response("ExpiredToken", "Token has expired", 400),
            json_response({"uri": "at://x"}),
        )

        result = client_for(pds).put_record(
            collection="net.atmowx.observation", rkey="abc", record=OBSERVATION
        )

        assert result["uri"] == "at://x"
        assert len(pds.calls_to(REFRESH)) == 1
        first, second = pds.calls_to(PUT)
        assert first.bearer != second.bearer

    def test_gives_up_after_one_auth_retry(self, pds: FakePds) -> None:
        pds.on(REFRESH, json_response(session_response()))
        pds.on(PUT, error_response("ExpiredToken", status=400))

        with pytest.raises(XrpcError, match="ExpiredToken"):
            client_for(pds).put_record(
                collection="net.atmowx.observation", rkey="abc", record=OBSERVATION
            )

        # Two attempts, not an endless renew/retry cycle.
        assert len(pds.calls_to(PUT)) == 2

    def test_treats_a_401_as_a_token_problem(self, pds: FakePds) -> None:
        pds.on(REFRESH, json_response(session_response()))
        pds.on(PUT, error_response("InvalidToken", status=401), json_response({"uri": "at://x"}))

        result = client_for(pds).put_record(
            collection="net.atmowx.observation", rkey="abc", record=OBSERVATION
        )

        assert result["uri"] == "at://x"


class TestRetries:
    def test_retries_a_server_error(self, pds: FakePds) -> None:
        pds.on(
            PUT,
            error_response("InternalServerError", status=500),
            error_response("InternalServerError", status=502),
            json_response({"uri": "at://x"}),
        )

        result = client_for(pds).put_record(
            collection="net.atmowx.observation", rkey="abc", record=OBSERVATION
        )

        assert result["uri"] == "at://x"
        assert len(pds.calls_to(PUT)) == 3

    def test_retries_a_network_failure(self, pds: FakePds) -> None:
        attempts = {"count": 0}

        def flaky(_: object) -> httpx.Response:
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise httpx.ReadTimeout("timed out")
            return json_response({"uri": "at://x"})

        pds.on(PUT, flaky)

        result = client_for(pds).put_record(
            collection="net.atmowx.observation", rkey="abc", record=OBSERVATION
        )

        assert result["uri"] == "at://x"

    def test_honours_retry_after_when_rate_limited(self, pds: FakePds) -> None:
        pds.on(
            PUT,
            error_response("RateLimitExceeded", status=429, headers={"retry-after": "0.01"}),
            json_response({"uri": "at://x"}),
        )

        result = client_for(pds).put_record(
            collection="net.atmowx.observation", rkey="abc", record=OBSERVATION
        )

        assert result["uri"] == "at://x"

    def test_does_not_retry_a_bad_request(self, pds: FakePds) -> None:
        pds.on(PUT, error_response("InvalidRequest", "Invalid record", 400))

        with pytest.raises(XrpcError, match="InvalidRequest"):
            client_for(pds).put_record(
                collection="net.atmowx.observation", rkey="abc", record=OBSERVATION
            )

        # Retrying a malformed record would fail identically every time.
        assert len(pds.calls_to(PUT)) == 1

    def test_gives_up_after_the_retry_budget(self, pds: FakePds) -> None:
        pds.on(PUT, error_response("InternalServerError", status=500))

        with pytest.raises(XrpcError):
            client_for(pds, max_retries=2).put_record(
                collection="net.atmowx.observation", rkey="abc", record=OBSERVATION
            )

        assert len(pds.calls_to(PUT)) == 3

    def test_surfaces_a_persistent_network_failure(self, pds: FakePds) -> None:
        def explode(_: object) -> httpx.Response:
            raise httpx.ConnectError("no route to host")

        pds.on(PUT, explode)

        with pytest.raises(NetworkError):
            client_for(pds, max_retries=1).put_record(
                collection="net.atmowx.observation", rkey="abc", record=OBSERVATION
            )


class TestQueries:
    def test_sends_a_get_as_a_query(self, pds: FakePds) -> None:
        pds.on(GET, json_response({"uri": "at://x", "value": OBSERVATION}))

        client_for(pds).get_record(collection="net.atmowx.observation", rkey="abc")

        call = pds.calls_to(GET)[0]
        assert call.method == "GET"
        assert "rkey=abc" in call.url
        assert "repo=did%3Aplc%3Atest" in call.url

    def test_serializes_booleans_for_list_records(self, pds: FakePds) -> None:
        pds.on(LIST, json_response({"records": []}))

        client_for(pds).list_records(collection="net.atmowx.station", limit=5, reverse=True)

        url = pds.calls_to(LIST)[0].url
        assert "limit=5" in url
        assert "reverse=true" in url


class TestRetryAfterParsing:
    def test_reads_a_numeric_retry_after(self) -> None:
        assert parse_retry_after(httpx.Headers({"retry-after": "30"})) == 30

    def test_reads_a_ratelimit_reset_timestamp(self) -> None:
        assert parse_retry_after(
            httpx.Headers({"ratelimit-reset": "1000"}), now=940
        ) == pytest.approx(60)

    def test_returns_nothing_when_no_header_is_present(self) -> None:
        assert parse_retry_after(httpx.Headers({})) is None
