"""Test doubles: signed-looking JWTs and a stubbed PDS."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx


def _segment(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def make_jwt(
    *,
    expires_in_seconds: float = 3600,
    subject: str = "did:plc:test",
    scope: str = "com.atproto.access",
    jti: str | None = None,
    issued_at: float | None = None,
) -> str:
    """A structurally valid JWT with a bogus signature.

    The bridge only ever decodes tokens to schedule refreshes, so an unsigned
    token is enough to drive every code path.
    """
    now = time.time() if issued_at is None else issued_at
    claims: dict[str, Any] = {
        "sub": subject,
        "scope": scope,
        "iat": int(now),
        "exp": int(now + expires_in_seconds),
    }
    if jti is not None:
        claims["jti"] = jti
    return f"{_segment({'alg': 'ES256K', 'typ': 'JWT'})}.{_segment(claims)}.signature"


def did_doc(did: str = "did:plc:test", pds: str = "https://pds.example.com") -> dict[str, Any]:
    return {
        "id": did,
        "service": [
            {
                "id": "#atproto_pds",
                "type": "AtprotoPersonalDataServer",
                "serviceEndpoint": pds,
            }
        ],
    }


def session_response(
    *,
    did: str = "did:plc:test",
    handle: str = "station.example.com",
    access_expires_in: float = 3600,
    refresh_expires_in: float = 90 * 24 * 3600,
    pds: str | None = "https://pds.example.com",
    access_jti: str | None = None,
    refresh_jti: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "did": did,
        "handle": handle,
        "accessJwt": make_jwt(expires_in_seconds=access_expires_in, subject=did, jti=access_jti),
        "refreshJwt": make_jwt(
            expires_in_seconds=refresh_expires_in,
            subject=did,
            scope="com.atproto.refresh",
            jti=refresh_jti,
        ),
        "active": True,
    }
    if pds is not None:
        body["didDoc"] = did_doc(did, pds)
    return body


@dataclass(slots=True)
class RecordedCall:
    method: str
    url: str
    headers: dict[str, str]
    body: Any

    @property
    def endpoint(self) -> str:
        return self.url.rsplit("/xrpc/", 1)[-1].split("?", 1)[0]

    @property
    def bearer(self) -> str | None:
        value = self.headers.get("authorization")
        return value.removeprefix("Bearer ") if value else None


Responder = Callable[[RecordedCall], httpx.Response]


@dataclass
class FakePds:
    """An httpx transport that answers XRPC calls from a scripted queue.

    Each endpoint gets a list of responses consumed in order; the last one
    repeats, so "always succeed after the first failure" is easy to express.
    """

    routes: dict[str, list[httpx.Response | Responder]] = field(default_factory=dict)
    calls: list[RecordedCall] = field(default_factory=list)

    def on(self, endpoint: str, *responses: httpx.Response | Responder) -> FakePds:
        self.routes.setdefault(endpoint, []).extend(responses)
        return self

    def calls_to(self, endpoint: str) -> list[RecordedCall]:
        return [call for call in self.calls if call.endpoint == endpoint]

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self._handle), timeout=5)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body: Any = None
        if request.content:
            try:
                body = json.loads(request.content)
            except json.JSONDecodeError:
                body = request.content.decode("utf-8", errors="replace")

        call = RecordedCall(
            method=request.method,
            url=str(request.url),
            headers={key.lower(): value for key, value in request.headers.items()},
            body=body,
        )
        self.calls.append(call)

        queued = self.routes.get(call.endpoint)
        if not queued:
            return json_response({"error": "MethodNotImplemented", "message": call.endpoint}, 501)

        # The final entry repeats so a route can describe steady-state behaviour.
        response = queued[0] if len(queued) == 1 else queued.pop(0)
        return response(call) if callable(response) else response


def json_response(
    body: Any, status: int = 200, headers: dict[str, str] | None = None
) -> httpx.Response:
    return httpx.Response(status, json=body, headers=headers or {})


def error_response(
    kind: str,
    message: str = "",
    status: int = 400,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    return json_response({"error": kind, "message": message or kind}, status, headers)
