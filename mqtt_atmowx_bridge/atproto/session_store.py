"""Where the session's tokens live between runs."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass(slots=True)
class StoredSession:
    did: str
    handle: str
    #: The account's own PDS, resolved from its DID document at login.
    pds_url: str
    #: The service the session was created against (may be an entryway).
    service: str
    access_jwt: str
    refresh_jwt: str
    saved_at: str
    #: Cached from the tokens so a malformed JWT does not hide the expiry.
    access_expires_at: str | None = None
    refresh_expires_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StoredSession | None:
        required = ("did", "access_jwt", "refresh_jwt", "pds_url")
        if not all(isinstance(data.get(key), str) for key in required):
            return None
        known = {field for field in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in data.items() if key in known})


class SessionStore(Protocol):
    """Somewhere to keep a session across restarts."""

    @property
    def description(self) -> str: ...

    def load(self) -> StoredSession | None: ...

    def save(self, session: StoredSession) -> None: ...

    def clear(self) -> None: ...


class MemorySessionStore:
    """A session that lives only as long as the process."""

    description = "memory"

    def __init__(self) -> None:
        self._session: StoredSession | None = None
        self._lock = threading.Lock()

    def load(self) -> StoredSession | None:
        with self._lock:
            return self._session

    def save(self, session: StoredSession) -> None:
        with self._lock:
            self._session = session

    def clear(self) -> None:
        with self._lock:
            self._session = None


class FileSessionStore:
    """Tokens on disk, owner-readable only.

    Writes go to a temp file and are renamed into place: a refresh that is
    interrupted mid-write must not leave behind a truncated file, because the
    rotated refresh token it contains is the only way back into the account
    without re-authenticating.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path).expanduser().resolve()
        self._lock = threading.Lock()

    @property
    def description(self) -> str:
        return str(self.path)

    def load(self) -> StoredSession | None:
        with self._lock:
            try:
                raw = self.path.read_text(encoding="utf-8")
            except FileNotFoundError:
                return None
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                return None
            if not isinstance(data, dict):
                return None
            return StoredSession.from_dict(data)

    def save(self, session: StoredSession) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(
                dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp"
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    json.dump(session.to_dict(), handle, indent=2)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(temporary, 0o600)
                os.replace(temporary, self.path)
            except BaseException:
                Path(temporary).unlink(missing_ok=True)
                raise

    def clear(self) -> None:
        with self._lock:
            self.path.unlink(missing_ok=True)
