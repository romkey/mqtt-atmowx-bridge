"""A small authenticated XRPC client.

Every call goes out with a token the session manager believes is fresh. If the
PDS disagrees — clock skew, a revoked session, a token that expired between
check and send — the call is retried exactly once against a renewed session.
Transient failures (5xx, rate limits) get bounded exponential backoff instead.
"""

from __future__ import annotations

import random
import time
from typing import Any

import httpx

from ..logging_setup import BridgeLogger, null_logger
from .errors import NetworkError, XrpcError, xrpc_error_from
from .session import SessionManager

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_RETRIES = 4
DEFAULT_BASE_BACKOFF_SECONDS = 1.0
DEFAULT_MAX_BACKOFF_SECONDS = 60.0


class AtpClient:
    """XRPC calls against the account's PDS, with auth and retries handled."""

    def __init__(
        self,
        session: SessionManager,
        *,
        logger: BridgeLogger | None = None,
        client: httpx.Client | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        base_backoff_seconds: float = DEFAULT_BASE_BACKOFF_SECONDS,
        max_backoff_seconds: float = DEFAULT_MAX_BACKOFF_SECONDS,
    ) -> None:
        self.session = session
        self._log = logger if logger is not None else null_logger()
        self._client = client if client is not None else httpx.Client(timeout=timeout_seconds)
        self._owns_client = client is None
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._base_backoff = base_backoff_seconds
        self._max_backoff = max_backoff_seconds

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def call(
        self,
        method: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        kind: str | None = None,
    ) -> dict[str, Any]:
        """Call an XRPC method, renewing the session and retrying as needed."""
        auth_retried = False
        attempt = 0

        while True:
            session = self.session.session()
            try:
                return self._request(
                    method,
                    base_url=session.pds_url,
                    access_jwt=session.access_jwt,
                    params=params,
                    body=body,
                    kind=kind,
                )
            except XrpcError as error:
                if error.is_expired_token or error.is_invalid_token:
                    if auth_retried:
                        raise
                    auth_retried = True
                    self._log.warn(
                        "the PDS rejected our access token; renewing the session",
                        method=method,
                        kind=error.kind,
                    )
                    self.session.invalidate(
                        session.access_jwt, reason=f"{method} returned {error.kind}"
                    )
                    continue

                if not error.is_retryable or attempt >= self._max_retries:
                    raise
                wait = self._backoff_for(attempt, error)
                attempt += 1
                self._log.warn(
                    "request failed; retrying",
                    method=method,
                    attempt=attempt,
                    wait_seconds=round(wait, 2),
                    err=str(error),
                )
                time.sleep(wait)
            except NetworkError as error:
                if attempt >= self._max_retries:
                    raise
                wait = self._backoff_for(attempt, error)
                attempt += 1
                self._log.warn(
                    "request failed; retrying",
                    method=method,
                    attempt=attempt,
                    wait_seconds=round(wait, 2),
                    err=str(error),
                )
                time.sleep(wait)

    def _backoff_for(self, attempt: int, error: Exception) -> float:
        if isinstance(error, XrpcError) and error.retry_after is not None:
            return min(error.retry_after, self._max_backoff)
        exponential = min(self._base_backoff * 2.0**attempt, self._max_backoff)
        # Full jitter, so a fleet of bridges does not retry in lockstep.
        return exponential * (0.5 + random.random() / 2)

    def _request(
        self,
        method: str,
        *,
        base_url: str,
        access_jwt: str,
        params: dict[str, Any] | None,
        body: dict[str, Any] | None,
        kind: str | None,
    ) -> dict[str, Any]:
        url = f"{base_url}/xrpc/{method}"
        headers = {"accept": "application/json", "authorization": f"Bearer {access_jwt}"}
        is_procedure = kind == "procedure" or body is not None

        query = None
        if params:
            query = {key: _query_value(value) for key, value in params.items() if value is not None}

        try:
            if is_procedure:
                response = self._client.post(
                    url, headers=headers, json=body, params=query, timeout=self._timeout
                )
            else:
                response = self._client.get(
                    url, headers=headers, params=query, timeout=self._timeout
                )
        except httpx.HTTPError as error:
            raise NetworkError(method, error) from error

        if response.status_code >= 400:
            raise xrpc_error_from(method, response)
        if response.status_code == 204 or not response.content:
            return {}
        parsed = response.json()
        return parsed if isinstance(parsed, dict) else {}

    # -- repo operations ----------------------------------------------------

    def put_record(
        self,
        *,
        collection: str,
        rkey: str,
        record: dict[str, Any],
        validate: bool | None = None,
        swap_record: str | None = None,
    ) -> dict[str, Any]:
        """Write a record at a known key.

        Observations use deterministic keys, so a republished reading overwrites
        itself rather than appearing twice.
        """
        session = self.session.session()
        body: dict[str, Any] = {
            "repo": session.did,
            "collection": collection,
            "rkey": rkey,
            "record": record,
        }
        # Left unset by default: a PDS that has not seen the net.atmowx
        # lexicons rejects `validate: true` outright.
        if validate is not None:
            body["validate"] = validate
        if swap_record is not None:
            body["swapRecord"] = swap_record

        return self.call("com.atproto.repo.putRecord", body=body, kind="procedure")

    def create_record(
        self,
        *,
        collection: str,
        record: dict[str, Any],
        rkey: str | None = None,
        validate: bool | None = None,
    ) -> dict[str, Any]:
        session = self.session.session()
        body: dict[str, Any] = {
            "repo": session.did,
            "collection": collection,
            "record": record,
        }
        if rkey is not None:
            body["rkey"] = rkey
        if validate is not None:
            body["validate"] = validate

        return self.call("com.atproto.repo.createRecord", body=body, kind="procedure")

    def get_record(self, *, collection: str, rkey: str, repo: str | None = None) -> dict[str, Any]:
        session = self.session.session()
        return self.call(
            "com.atproto.repo.getRecord",
            params={"repo": repo or session.did, "collection": collection, "rkey": rkey},
        )

    def list_records(
        self,
        *,
        collection: str,
        limit: int = 50,
        cursor: str | None = None,
        reverse: bool | None = None,
        repo: str | None = None,
    ) -> dict[str, Any]:
        session = self.session.session()
        return self.call(
            "com.atproto.repo.listRecords",
            params={
                "repo": repo or session.did,
                "collection": collection,
                "limit": limit,
                "cursor": cursor,
                "reverse": reverse,
            },
        )


def _query_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
