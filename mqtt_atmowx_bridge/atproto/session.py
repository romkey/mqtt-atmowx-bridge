"""atproto session lifecycle: log in, keep the access token fresh, survive
restarts, and never lose a rotated refresh token.

The rules this implements, which are the ones that bite in practice:

* Access tokens are short-lived (a couple of hours). Refresh *before* expiry
  rather than waiting for a 400/ExpiredToken, so a publish is not delayed by a
  round trip that was always going to fail.
* ``com.atproto.server.refreshSession`` **rotates** the refresh token: the
  response carries a new one and the old one is spent. It is persisted before
  the new access token is used, because losing it means falling back to the app
  password, and replaying a spent one looks like token theft.
* Renewals are single-flight. A dozen concurrent publishes hitting an expired
  token must produce one refresh, not a dozen racing rotations that invalidate
  each other. Callers serialize on one lock and re-check on the way in, so
  whoever arrives second gets the token the first one fetched.
* Refresh tokens expire too (~90 days). If ours is gone, or the PDS rejects it,
  fall back to a full ``createSession`` with the app password, with backoff so a
  takedown or a bad password does not turn into a login flood.
* Write to the account's own PDS, resolved from the DID document at login, not
  to whatever entryway we authenticated against.
"""

from __future__ import annotations

import random
import re
import threading
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx

from ..logging_setup import BridgeLogger, null_logger
from .errors import NetworkError, XrpcError, xrpc_error_from
from .jwt import describe_token, expires_at, is_expired
from .session_store import MemorySessionStore, SessionStore, StoredSession

DEFAULT_REFRESH_SKEW_SECONDS = 300.0
DEFAULT_REFRESH_FLOOR_SECONDS = 24 * 60 * 60.0
DEFAULT_TIMEOUT_SECONDS = 30.0
MIN_LOGIN_BACKOFF_SECONDS = 5.0
MAX_LOGIN_BACKOFF_SECONDS = 15 * 60.0

_APP_PASSWORD_PATTERN = re.compile(r"^[a-z0-9]{4}(-[a-z0-9]{4}){3}$", re.IGNORECASE)


class SessionError(Exception):
    """The session cannot be established or renewed."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class SessionManager:
    """Holds an app password and trades it for a session, keeping it fresh."""

    def __init__(
        self,
        *,
        service: str,
        identifier: str,
        password: str,
        store: SessionStore | None = None,
        logger: BridgeLogger | None = None,
        refresh_skew_seconds: float = DEFAULT_REFRESH_SKEW_SECONDS,
        refresh_token_floor_seconds: float = DEFAULT_REFRESH_FLOOR_SECONDS,
        client: httpx.Client | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.service = normalize_service_url(service)
        self.identifier = identifier

        self._password = password
        self._store: SessionStore = store if store is not None else MemorySessionStore()
        self._log = logger if logger is not None else null_logger()
        self._refresh_skew = refresh_skew_seconds
        self._refresh_floor = refresh_token_floor_seconds
        self._timeout = timeout_seconds
        self._client = client if client is not None else httpx.Client(timeout=timeout_seconds)
        self._owns_client = client is None

        self._session: StoredSession | None = None
        self._loaded = False
        # Single-flight: everything that can renew holds this lock, so a burst
        # of callers produces one login or one rotation.
        self._lock = threading.RLock()
        self._login_failures = 0
        self._next_login_allowed_at = 0.0

    # -- public API ---------------------------------------------------------

    @property
    def did(self) -> str | None:
        """The DID we are publishing as, once a session exists."""
        session = self._session
        return session.did if session else None

    @property
    def pds_url(self) -> str | None:
        """The host to send repo writes to."""
        session = self._session
        return session.pds_url if session else None

    def session(self) -> StoredSession:
        """A usable session, refreshing or logging in as needed."""
        with self._lock:
            self._load_once()
            current = self._session
            if current is not None and not is_expired(current.access_jwt, self._refresh_skew):
                return current
            reason = "access token is expiring" if current else "no session yet"
            return self._renew(reason)

    def access_token(self) -> str:
        return self.session().access_jwt

    def invalidate(
        self,
        stale_access_jwt: str | None = None,
        reason: str = "server rejected the access token",
    ) -> StoredSession:
        """Force a renewal after the PDS rejected a token we thought was good.

        Passing the token that failed makes this idempotent under concurrency:
        if another thread has already renewed, the stale token is no longer
        current and there is nothing to do.
        """
        with self._lock:
            self._load_once()
            current = self._session
            if (
                stale_access_jwt is not None
                and current is not None
                and current.access_jwt != stale_access_jwt
            ):
                return current
            return self._renew(reason)

    def logout(self) -> None:
        """Revoke the session on the server and forget it locally."""
        with self._lock:
            self._load_once()
            session = self._session
            self._session = None
            self._store.clear()
            if session is None:
                return
            try:
                self._post(
                    "com.atproto.server.deleteSession",
                    base_url=session.pds_url,
                    bearer=session.refresh_jwt,
                )
            except (XrpcError, NetworkError) as error:
                # The tokens are already gone locally; a failed revoke is not fatal.
                self._log.warn("could not revoke the session on the server", err=str(error))

    def status(self) -> dict[str, Any]:
        """Redacted session state, for logs and the health endpoint."""
        session = self._session
        if session is None:
            return {"authenticated": False}

        expires_in: float | None = None
        if session.access_expires_at:
            expires_in = round(
                datetime.fromisoformat(session.access_expires_at).timestamp() - time.time()
            )
        return {
            "authenticated": True,
            "did": session.did,
            "handle": session.handle,
            "pds": session.pds_url,
            "access_expires_at": session.access_expires_at,
            "refresh_expires_at": session.refresh_expires_at,
            "access_expires_in_seconds": expires_in,
        }

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # -- renewal ------------------------------------------------------------

    def _load_once(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            stored = self._store.load()
        except OSError as error:
            self._log.warn("could not read the stored session", err=str(error))
            return
        if stored is None:
            return
        # A stored session for a different account is not ours to use.
        if stored.service != self.service:
            self._log.warn(
                "stored session is for a different service; ignoring it",
                stored=stored.service,
                configured=self.service,
            )
            return
        self._session = stored
        self._log.info(
            f"resumed session from {self._store.description}",
            did=stored.did,
            handle=stored.handle,
            **describe_token(stored.access_jwt),
        )

    def _renew(self, reason: str) -> StoredSession:
        current = self._session

        if current is not None and self._refresh_token_is_usable(current):
            self._log.debug("refreshing the session", reason=reason)
            try:
                return self._refresh(current)
            except NetworkError:
                # A network blip says nothing about the refresh token's
                # validity. Surface it and try again later rather than spending
                # the app password on what is probably a transient failure.
                raise
            except XrpcError as error:
                if error.is_retryable:
                    raise
                self._log.warn("refresh failed; falling back to a full login", err=str(error))
            except SessionError as error:
                self._log.warn(
                    "refresh returned something unexpected; falling back to a full login",
                    err=str(error),
                )

        return self._login(reason)

    def _refresh_token_is_usable(self, session: StoredSession) -> bool:
        if is_expired(session.refresh_jwt, self._refresh_floor):
            self._log.info("refresh token is at or near expiry; logging in again")
            return False
        return True

    def _refresh(self, current: StoredSession) -> StoredSession:
        response = self._post(
            "com.atproto.server.refreshSession",
            base_url=current.pds_url,
            bearer=current.refresh_jwt,
        )

        did = response.get("did")
        if did != current.did:
            raise SessionError(
                f"refresh returned a different DID ({did} rather than {current.did})",
                retryable=False,
            )

        # The old refresh token is spent from here on. Persist the new one
        # before anything else so a crash cannot strand us between two tokens.
        session = self._to_stored_session(response, current.service, current.pds_url)
        self._persist(session)

        self._log.info("session refreshed", did=session.did, **describe_token(session.access_jwt))
        return session

    def _login(self, reason: str) -> StoredSession:
        wait = self._next_login_allowed_at - time.monotonic()
        if wait > 0:
            self._log.debug("waiting out the login backoff", wait_seconds=round(wait, 1))
            time.sleep(wait)

        try:
            response = self._post(
                "com.atproto.server.createSession",
                base_url=self.service,
                body={"identifier": self.identifier, "password": self._password},
            )

            if response.get("active") is False:
                status = response.get("status")
                raise SessionError(
                    f"account {response.get('did')} is not active"
                    + (f" ({status})" if status else ""),
                    retryable=False,
                )

            pds_url = pds_from_did_doc(response.get("didDoc")) or self.service
            session = self._to_stored_session(response, self.service, pds_url)
            self._persist(session)

            self._login_failures = 0
            self._next_login_allowed_at = 0.0
            self._log.info(
                "logged in",
                did=session.did,
                handle=session.handle,
                pds=session.pds_url,
                reason=reason,
            )
            return session
        except (XrpcError, NetworkError, SessionError) as error:
            self._note_login_failure(error)
            raise

    def _note_login_failure(self, error: Exception) -> None:
        self._login_failures += 1
        backoff = min(
            MIN_LOGIN_BACKOFF_SECONDS * 2 ** (self._login_failures - 1),
            MAX_LOGIN_BACKOFF_SECONDS,
        )
        wait = backoff * (0.5 + random.random() / 2)

        if isinstance(error, XrpcError):
            # A rate-limited PDS tells us exactly how long to sit out.
            if error.retry_after is not None:
                wait = max(wait, error.retry_after)
            # Bad credentials will not fix themselves; do not hammer the endpoint.
            if error.status == 401 or error.kind == "AuthenticationRequired":
                wait = max(wait, MAX_LOGIN_BACKOFF_SECONDS)
                self._log.error(
                    "login was rejected — check the identifier and app password",
                    kind=error.kind,
                )
        self._next_login_allowed_at = time.monotonic() + wait

    def _to_stored_session(
        self, response: dict[str, Any], service: str, pds_url: str
    ) -> StoredSession:
        return StoredSession(
            did=str(response["did"]),
            handle=str(response.get("handle", "")),
            pds_url=normalize_service_url(pds_url),
            service=service,
            access_jwt=str(response["accessJwt"]),
            refresh_jwt=str(response["refreshJwt"]),
            saved_at=datetime.now(UTC).isoformat(),
            access_expires_at=_safe_expiry(str(response["accessJwt"])),
            refresh_expires_at=_safe_expiry(str(response["refreshJwt"])),
        )

    def _persist(self, session: StoredSession) -> None:
        self._session = session
        try:
            self._store.save(session)
        except OSError as error:
            # We still hold the token in memory, so publishing continues, but
            # the next restart will have to log in again. Worth shouting about.
            self._log.error(
                "could not persist the session; a restart will need the app password again",
                err=str(error),
                store=self._store.description,
            )

    # -- transport ----------------------------------------------------------

    def _post(
        self,
        method: str,
        *,
        base_url: str,
        bearer: str | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{normalize_service_url(base_url)}/xrpc/{method}"
        headers = {"accept": "application/json"}
        if bearer:
            headers["authorization"] = f"Bearer {bearer}"

        try:
            response = self._client.post(url, headers=headers, json=body, timeout=self._timeout)
        except httpx.HTTPError as error:
            raise NetworkError(method, error) from error

        if response.status_code >= 400:
            raise xrpc_error_from(method, response)
        if response.status_code == 204 or not response.content:
            return {}
        parsed = response.json()
        return parsed if isinstance(parsed, dict) else {}


def _safe_expiry(token: str) -> str | None:
    try:
        expiry = expires_at(token)
    except ValueError:
        return None
    return expiry.isoformat() if expiry else None


def normalize_service_url(url: str) -> str:
    """The scheme and host of a service URL, with any path stripped."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"service URL must be http(s): {url}")
    if not parsed.netloc:
        raise ValueError(f"service URL has no host: {url}")
    return urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))


def pds_from_did_doc(did_doc: Any) -> str | None:
    """The account's PDS, per its DID document.

    Logging in at an entryway (bsky.social) hands back a DID doc pointing at the
    PDS that actually holds the repo, and that is where writes belong.
    """
    if not isinstance(did_doc, dict):
        return None
    services = did_doc.get("service")
    if not isinstance(services, list):
        return None

    for entry in services:
        if not isinstance(entry, dict):
            continue
        identifier = entry.get("id")
        is_pds = (isinstance(identifier, str) and identifier.endswith("#atproto_pds")) or entry.get(
            "type"
        ) == "AtprotoPersonalDataServer"
        endpoint = entry.get("serviceEndpoint")
        if is_pds and isinstance(endpoint, str):
            try:
                return normalize_service_url(endpoint)
            except ValueError:
                return None
    return None


def looks_like_app_password(password: str) -> bool:
    """App passwords look like ``xxxx-xxxx-xxxx-xxxx``."""
    return bool(_APP_PASSWORD_PATTERN.match(password))
