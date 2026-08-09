"""Errors returned by an XRPC endpoint, in the shape atproto defines."""

from __future__ import annotations

import email.utils
import time

import httpx


class XrpcError(Exception):
    """A non-2xx response from an XRPC endpoint."""

    def __init__(
        self,
        *,
        status: int,
        kind: str,
        message: str,
        endpoint: str,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(f"{endpoint} failed: {status} {kind}: {message}")
        self.status = status
        #: The ``error`` name from the response body, e.g. ``ExpiredToken``.
        self.kind = kind
        self.detail = message
        self.endpoint = endpoint
        #: Seconds to wait before retrying, from ``retry-after`` or ``ratelimit-reset``.
        self.retry_after = retry_after

    @property
    def is_expired_token(self) -> bool:
        """The access token is stale but the session may still be refreshable."""
        return self.kind == "ExpiredToken"

    @property
    def is_invalid_token(self) -> bool:
        """The token cannot be used at all — refreshing will not help; log in again."""
        return self.kind in {"InvalidToken", "AuthMissing"} or self.status == 401

    @property
    def is_account_problem(self) -> bool:
        """The account itself is unusable: no amount of retrying fixes this."""
        return self.kind in {"AccountTakedown", "AccountDeactivated", "AuthFactorTokenRequired"}

    @property
    def is_rate_limited(self) -> bool:
        return self.status == 429 or self.kind == "RateLimitExceeded"

    @property
    def is_retryable(self) -> bool:
        return self.status >= 500 or self.is_rate_limited or self.status == 408


class NetworkError(Exception):
    """A transport-level failure: DNS, TCP, TLS, timeout. Always worth retrying."""

    def __init__(self, endpoint: str, cause: BaseException) -> None:
        super().__init__(f"{endpoint} failed: {cause}")
        self.endpoint = endpoint
        self.__cause__ = cause


def parse_retry_after(headers: httpx.Headers, now: float | None = None) -> float | None:
    """Seconds to wait, from whichever rate-limit header the server sent."""
    moment = time.time() if now is None else now

    retry_after = headers.get("retry-after")
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            parsed = email.utils.parsedate_to_datetime(retry_after)
            if parsed is not None:
                return max(0.0, parsed.timestamp() - moment)

    reset = headers.get("ratelimit-reset")
    if reset:
        try:
            return max(0.0, float(reset) - moment)
        except ValueError:
            return None
    return None


def xrpc_error_from(endpoint: str, response: httpx.Response) -> XrpcError:
    body: dict[str, object] = {}
    try:
        parsed = response.json()
        if isinstance(parsed, dict):
            body = parsed
    except ValueError:
        # Non-JSON error bodies (proxies, gateways) still tell us the status.
        pass

    kind = body.get("error")
    message = body.get("message")
    return XrpcError(
        status=response.status_code,
        kind=str(kind) if isinstance(kind, str) else f"HTTP{response.status_code}",
        message=str(message) if isinstance(message, str) else response.reason_phrase,
        endpoint=endpoint,
        retry_after=parse_retry_after(response.headers),
    )
