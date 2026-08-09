"""Session lifecycle: login, refresh, rotation, persistence, failure handling."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import httpx
import pytest

from mqtt_atmowx_bridge.atproto.errors import NetworkError, XrpcError
from mqtt_atmowx_bridge.atproto.jwt import decode_jwt, describe_token, is_expired
from mqtt_atmowx_bridge.atproto.session import (
    SessionError,
    SessionManager,
    looks_like_app_password,
    normalize_service_url,
    pds_from_did_doc,
)
from mqtt_atmowx_bridge.atproto.session_store import (
    FileSessionStore,
    MemorySessionStore,
    StoredSession,
)

from .helpers import FakePds, error_response, json_response, make_jwt, session_response

CREATE = "com.atproto.server.createSession"
REFRESH = "com.atproto.server.refreshSession"
DELETE = "com.atproto.server.deleteSession"


def manager(pds: FakePds, store: object | None = None, **kwargs: object) -> SessionManager:
    return SessionManager(
        service="https://bsky.social",
        identifier="station.example.com",
        password="abcd-efgh-ijkl-mnop",
        store=store or MemorySessionStore(),  # type: ignore[arg-type]
        client=pds.client(),
        **kwargs,  # type: ignore[arg-type]
    )


class TestLogin:
    def test_exchanges_the_app_password_for_a_session(self) -> None:
        pds = FakePds().on(CREATE, json_response(session_response()))

        session = manager(pds).session()

        assert session.did == "did:plc:test"
        assert session.handle == "station.example.com"
        assert pds.calls_to(CREATE)[0].body == {
            "identifier": "station.example.com",
            "password": "abcd-efgh-ijkl-mnop",
        }

    def test_writes_to_the_pds_from_the_did_document_not_the_entryway(self) -> None:
        pds = FakePds().on(CREATE, json_response(session_response(pds="https://pds.example.com")))

        session = manager(pds).session()

        # We authenticated at bsky.social but the repo lives elsewhere.
        assert session.service == "https://bsky.social"
        assert session.pds_url == "https://pds.example.com"

    def test_falls_back_to_the_service_when_there_is_no_did_document(self) -> None:
        pds = FakePds().on(CREATE, json_response(session_response(pds=None)))

        assert manager(pds).session().pds_url == "https://bsky.social"

    def test_reuses_the_session_instead_of_logging_in_again(self) -> None:
        pds = FakePds().on(CREATE, json_response(session_response()))
        subject = manager(pds)

        first = subject.session()
        second = subject.session()

        assert first.access_jwt == second.access_jwt
        assert len(pds.calls_to(CREATE)) == 1

    def test_refuses_a_deactivated_account(self) -> None:
        pds = FakePds().on(
            CREATE,
            json_response({**session_response(), "active": False, "status": "deactivated"}),
        )

        with pytest.raises(SessionError, match="not active"):
            manager(pds).session()

    def test_surfaces_bad_credentials(self) -> None:
        pds = FakePds().on(
            CREATE,
            error_response("AuthenticationRequired", "Invalid identifier or password", 401),
        )

        with pytest.raises(XrpcError) as caught:
            manager(pds).session()
        assert caught.value.status == 401

    def test_backs_off_after_a_rejected_login(self) -> None:
        pds = FakePds().on(CREATE, error_response("AuthenticationRequired", status=401))
        subject = manager(pds)

        with pytest.raises(XrpcError):
            subject.session()

        # The backoff for a 401 is the full ceiling, so the next attempt is not
        # made immediately; assert we recorded that rather than sleeping for it.
        assert subject._next_login_allowed_at > 0


class TestRefresh:
    def test_refreshes_before_the_access_token_expires(self) -> None:
        pds = (
            FakePds()
            .on(CREATE, json_response(session_response(access_expires_in=60)))
            .on(REFRESH, json_response(session_response(access_expires_in=3600)))
        )
        # A token expiring in 60s is inside the 300s skew, so the next call
        # refreshes rather than using it.
        subject = manager(pds, refresh_skew_seconds=300)

        first = subject.session()
        second = subject.session()

        assert len(pds.calls_to(REFRESH)) == 1
        assert second.access_jwt != first.access_jwt

    def test_authenticates_the_refresh_with_the_refresh_token(self) -> None:
        created = session_response(access_expires_in=-10)
        pds = (
            FakePds()
            .on(CREATE, json_response(created))
            .on(REFRESH, json_response(session_response()))
        )
        subject = manager(pds)

        subject.session()
        subject.session()

        assert pds.calls_to(REFRESH)[0].bearer == created["refreshJwt"]

    def test_stores_the_rotated_refresh_token(self) -> None:
        pds = (
            FakePds()
            .on(CREATE, json_response(session_response(access_expires_in=-10, refresh_jti="one")))
            .on(REFRESH, json_response(session_response(refresh_jti="two")))
        )
        store = MemorySessionStore()
        subject = manager(pds, store=store)

        subject.session()
        subject.session()

        saved = store.load()
        assert saved is not None
        # The spent token must not be what we keep.
        assert decode_jwt(saved.refresh_jwt)["jti"] == "two"

    def test_refuses_a_refresh_that_returns_a_different_account(self) -> None:
        pds = (
            FakePds()
            .on(
                CREATE,
                json_response(session_response(access_expires_in=-10)),
                json_response(session_response()),
            )
            .on(REFRESH, json_response(session_response(did="did:plc:someone-else")))
        )
        subject = manager(pds)
        subject.session()

        renewed = subject.session()

        # Trusting it would mean publishing into someone else's repo, so the
        # response is discarded and we authenticate from scratch.
        assert renewed.did == "did:plc:test"
        assert len(pds.calls_to(CREATE)) == 2

    def test_logs_in_again_when_the_refresh_token_is_rejected(self) -> None:
        pds = (
            FakePds()
            .on(
                CREATE,
                json_response(session_response(access_expires_in=-10)),
                json_response(session_response()),
            )
            .on(REFRESH, error_response("ExpiredToken", "Token has expired", 400))
        )
        subject = manager(pds)

        subject.session()
        renewed = subject.session()

        assert len(pds.calls_to(CREATE)) == 2
        assert not is_expired(renewed.access_jwt)

    def test_does_not_spend_the_app_password_on_a_network_blip(self) -> None:
        def explode(_: object) -> object:
            raise httpx.ConnectError("connection reset")

        pds = FakePds().on(CREATE, json_response(session_response(access_expires_in=-10)))
        pds.on(REFRESH, explode)  # type: ignore[arg-type]
        subject = manager(pds)
        subject.session()

        with pytest.raises(NetworkError):
            subject.session()

        # One login, not two: the refresh token may still be perfectly good.
        assert len(pds.calls_to(CREATE)) == 1

    def test_logs_in_again_when_the_refresh_token_is_near_expiry(self) -> None:
        pds = (
            FakePds()
            .on(
                CREATE,
                json_response(session_response(access_expires_in=-10, refresh_expires_in=3600)),
                json_response(session_response()),
            )
            .on(REFRESH, error_response("ShouldNotBeCalled", status=500))
        )
        subject = manager(pds)

        subject.session()
        subject.session()

        # The refresh token has under the 24h floor left, so we skip straight
        # to a login instead of racing its expiry.
        assert len(pds.calls_to(REFRESH)) == 0
        assert len(pds.calls_to(CREATE)) == 2


class TestConcurrency:
    def test_a_burst_of_callers_produces_one_login(self) -> None:
        started = threading.Barrier(8)
        pds = FakePds().on(CREATE, json_response(session_response()))
        subject = manager(pds)
        tokens: list[str] = []
        lock = threading.Lock()

        def worker() -> None:
            started.wait()
            token = subject.access_token()
            with lock:
                tokens.append(token)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        assert len(pds.calls_to(CREATE)) == 1
        assert len(set(tokens)) == 1

    def test_a_burst_of_rejections_produces_one_refresh(self) -> None:
        pds = (
            FakePds()
            .on(CREATE, json_response(session_response()))
            .on(REFRESH, json_response(session_response(access_jti="renewed")))
        )
        subject = manager(pds)
        stale = subject.access_token()

        started = threading.Barrier(6)

        def worker() -> None:
            started.wait()
            subject.invalidate(stale)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        # Every thread saw the same stale token; only the first renews.
        assert len(pds.calls_to(REFRESH)) == 1

    def test_invalidate_is_a_no_op_once_someone_else_has_renewed(self) -> None:
        pds = (
            FakePds()
            .on(CREATE, json_response(session_response()))
            .on(REFRESH, json_response(session_response()))
        )
        subject = manager(pds)
        subject.access_token()

        subject.invalidate("a-token-we-no-longer-hold")

        assert len(pds.calls_to(REFRESH)) == 0


class TestPersistence:
    def test_resumes_a_saved_session_without_logging_in(self, tmp_path: Path) -> None:
        store = FileSessionStore(tmp_path / "session.json")
        store.save(
            StoredSession(
                did="did:plc:test",
                handle="station.example.com",
                pds_url="https://pds.example.com",
                service="https://bsky.social",
                access_jwt=make_jwt(expires_in_seconds=3600),
                refresh_jwt=make_jwt(expires_in_seconds=90 * 24 * 3600),
                saved_at="2026-08-07T00:00:00Z",
            )
        )
        pds = FakePds().on(CREATE, error_response("ShouldNotBeCalled", status=500))

        session = manager(pds, store=FileSessionStore(tmp_path / "session.json")).session()

        assert session.did == "did:plc:test"
        assert len(pds.calls_to(CREATE)) == 0

    def test_ignores_a_session_saved_against_a_different_service(self, tmp_path: Path) -> None:
        store = FileSessionStore(tmp_path / "session.json")
        store.save(
            StoredSession(
                did="did:plc:test",
                handle="station.example.com",
                pds_url="https://other.example.com",
                service="https://other.example.com",
                access_jwt=make_jwt(),
                refresh_jwt=make_jwt(expires_in_seconds=90 * 24 * 3600),
                saved_at="2026-08-07T00:00:00Z",
            )
        )
        pds = FakePds().on(CREATE, json_response(session_response()))

        manager(pds, store=FileSessionStore(tmp_path / "session.json")).session()

        assert len(pds.calls_to(CREATE)) == 1

    def test_writes_the_session_file_owner_only(self, tmp_path: Path) -> None:
        path = tmp_path / "session.json"
        pds = FakePds().on(CREATE, json_response(session_response()))

        manager(pds, store=FileSessionStore(path)).session()

        assert path.exists()
        assert path.stat().st_mode & 0o777 == 0o600
        assert json.loads(path.read_text())["did"] == "did:plc:test"

    def test_survives_a_corrupt_session_file(self, tmp_path: Path) -> None:
        path = tmp_path / "session.json"
        path.write_text("{not json at all")
        pds = FakePds().on(CREATE, json_response(session_response()))

        assert manager(pds, store=FileSessionStore(path)).session().did == "did:plc:test"

    def test_ignores_a_session_file_missing_its_tokens(self, tmp_path: Path) -> None:
        path = tmp_path / "session.json"
        path.write_text(json.dumps({"did": "did:plc:test"}))

        assert FileSessionStore(path).load() is None


class TestLogout:
    def test_revokes_the_session_and_removes_the_file(self, tmp_path: Path) -> None:
        path = tmp_path / "session.json"
        pds = (
            FakePds()
            .on(CREATE, json_response(session_response()))
            .on(DELETE, json_response({}, 200))
        )
        subject = manager(pds, store=FileSessionStore(path))
        created = subject.session()

        subject.logout()

        assert pds.calls_to(DELETE)[0].bearer == created.refresh_jwt
        assert not path.exists()

    def test_forgets_the_session_even_if_the_server_call_fails(self, tmp_path: Path) -> None:
        path = tmp_path / "session.json"
        pds = (
            FakePds()
            .on(CREATE, json_response(session_response()))
            .on(DELETE, error_response("InternalServerError", status=500))
        )
        subject = manager(pds, store=FileSessionStore(path))
        subject.session()

        subject.logout()

        assert not path.exists()
        assert subject.status() == {"authenticated": False}


class TestHelpers:
    def test_reports_status_without_leaking_tokens(self) -> None:
        pds = FakePds().on(CREATE, json_response(session_response()))
        subject = manager(pds)
        subject.session()

        status = subject.status()

        assert status["authenticated"] is True
        assert status["did"] == "did:plc:test"
        assert "jwt" not in json.dumps(status).lower()

    def test_describe_token_omits_the_token(self) -> None:
        described = describe_token(make_jwt(subject="did:plc:test", jti="abc"))

        assert described["sub"] == "did:plc:test"
        assert described["expires_at"] is not None
        assert "signature" not in json.dumps(described)

    def test_normalizes_a_service_url(self) -> None:
        assert normalize_service_url("https://bsky.social/") == "https://bsky.social"
        assert normalize_service_url("https://pds.example.com/xrpc") == "https://pds.example.com"

    def test_rejects_a_service_url_that_is_not_http(self) -> None:
        with pytest.raises(ValueError, match="http"):
            normalize_service_url("mqtt://broker.local")

    def test_finds_the_pds_in_a_did_document(self) -> None:
        assert (
            pds_from_did_doc(
                {
                    "service": [
                        {"id": "#atproto_labeler", "serviceEndpoint": "https://labeler.example"},
                        {
                            "id": "#atproto_pds",
                            "type": "AtprotoPersonalDataServer",
                            "serviceEndpoint": "https://pds.example.com",
                        },
                    ]
                }
            )
            == "https://pds.example.com"
        )

    def test_returns_nothing_for_a_did_document_without_a_pds(self) -> None:
        assert pds_from_did_doc({"service": []}) is None
        assert pds_from_did_doc(None) is None

    @pytest.mark.parametrize(
        ("password", "expected"),
        [
            ("abcd-efgh-ijkl-mnop", True),
            ("3kf9-2ncd-88fa-0021", True),
            ("hunter2", False),
            ("abcd-efgh-ijkl", False),
        ],
    )
    def test_recognizes_an_app_password(self, password: str, expected: bool) -> None:
        assert looks_like_app_password(password) is expected
