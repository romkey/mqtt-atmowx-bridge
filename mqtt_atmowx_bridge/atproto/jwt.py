"""Just enough JWT handling to manage an atproto session.

We are the *bearer* of these tokens, not their verifier: the PDS signs them and
the PDS checks them. Decoding here is only used to schedule refreshes and to
sanity-check that a token belongs to the account we think it does, so it
deliberately does not verify the signature. Never make an authorization
decision on the strength of anything this module returns.
"""

from __future__ import annotations

import base64
import binascii
import json
import time
from datetime import UTC, datetime
from typing import Any


class MalformedJwtError(ValueError):
    """The token is not a well-formed JWT."""


def _decode_segment(segment: str) -> bytes:
    # JWT segments are base64url without padding.
    padding = "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(segment + padding)
    except (binascii.Error, ValueError) as error:
        raise MalformedJwtError(f"segment is not valid base64url: {error}") from error


def decode_jwt(token: str) -> dict[str, Any]:
    """Decode the claims of a JWT **without verifying its signature**."""
    parts = token.split(".")
    if len(parts) != 3:
        raise MalformedJwtError(f"expected 3 dot-separated segments, got {len(parts)}")

    try:
        payload = json.loads(_decode_segment(parts[1]))
    except json.JSONDecodeError as error:
        raise MalformedJwtError(f"payload is not JSON: {error}") from error

    if not isinstance(payload, dict):
        raise MalformedJwtError("payload is not a JSON object")
    return payload


def expires_at(token: str) -> datetime | None:
    """The token's expiry, or ``None`` if it does not carry one."""
    exp = decode_jwt(token).get("exp")
    if isinstance(exp, int | float):
        return datetime.fromtimestamp(float(exp), tz=UTC)
    return None


def is_expired(token: str, skew_seconds: float = 0.0, now: float | None = None) -> bool:
    """Whether the token is expired, or will be within ``skew_seconds``.

    A token with no ``exp`` is treated as expired: we would rather refresh
    needlessly than sit on something the PDS has already retired.
    """
    try:
        expiry = expires_at(token)
    except MalformedJwtError:
        return True
    if expiry is None:
        return True
    return expiry.timestamp() - skew_seconds <= (time.time() if now is None else now)


def describe_token(token: str) -> dict[str, Any]:
    """A redacted description of a token, safe to log.

    The claims that matter for debugging, and never the token itself.
    """
    try:
        claims = decode_jwt(token)
    except MalformedJwtError as error:
        return {"malformed": str(error)}

    exp = claims.get("exp")
    return {
        "sub": claims.get("sub"),
        "aud": claims.get("aud"),
        "scope": claims.get("scope"),
        "expires_at": (
            datetime.fromtimestamp(float(exp), tz=UTC).isoformat()
            if isinstance(exp, int | float)
            else None
        ),
        # `jti` identifies which refresh token we hold, which matters when
        # diagnosing rotation problems. It is not itself a credential.
        "jti": claims.get("jti"),
    }
