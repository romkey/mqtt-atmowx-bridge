"""TID (timestamp identifier) record keys.

A TID is 64 bits encoded as 13 characters of base32-sortable:

    bit 63     always 0
    bits 62-10 microseconds since the UNIX epoch
    bits 9-0   "clock identifier"

net.atmowx.observation asks for a key derived from ``observedAt``. We therefore
use clock identifier 0 rather than the random value the atproto spec suggests:
republishing the same reading then lands on the same key and overwrites itself
instead of creating a duplicate record.
"""

from __future__ import annotations

import random
import re
from datetime import UTC, datetime

ALPHABET = "234567abcdefghijklmnopqrstuvwxyz"
TID_LENGTH = 13
TID_PATTERN = re.compile(r"^[234567abcdefghijklmnopqrstuvwxyz]{13}$")

_CLOCK_ID_BITS = 10
_MAX_CLOCK_ID = (1 << _CLOCK_ID_BITS) - 1
_MAX_MICROSECONDS = (1 << 53) - 1

_DECODE = {character: index for index, character in enumerate(ALPHABET)}


def encode_tid(microseconds: int, clock_id: int = 0) -> str:
    """Encode microseconds-since-epoch and a clock identifier as a TID."""
    if not 0 <= microseconds <= _MAX_MICROSECONDS:
        raise ValueError(f"timestamp out of TID range: {microseconds}")
    if not 0 <= clock_id <= _MAX_CLOCK_ID:
        raise ValueError(f"clock identifier out of range: {clock_id}")

    value = (microseconds << _CLOCK_ID_BITS) | clock_id
    characters = []
    for _ in range(TID_LENGTH):
        characters.append(ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(characters))


def decode_tid(tid: str) -> tuple[int, int]:
    """Return ``(microseconds, clock_id)`` for a TID."""
    if not TID_PATTERN.match(tid):
        raise ValueError(f"not a TID: {tid}")

    value = 0
    for character in tid:
        value = (value << 5) | _DECODE[character]
    return value >> _CLOCK_ID_BITS, value & _MAX_CLOCK_ID


def tid_from_datetime(observed_at: datetime) -> str:
    """The record key for an observation taken at ``observed_at``.

    Deterministic, so re-publishing a reading is an overwrite rather than a
    duplicate.
    """
    if observed_at.tzinfo is None:
        raise ValueError("observed_at must be timezone-aware")
    microseconds = int(observed_at.timestamp() * 1_000_000)
    return encode_tid(microseconds)


def datetime_from_tid(tid: str) -> datetime:
    microseconds, _ = decode_tid(tid)
    return datetime.fromtimestamp(microseconds / 1_000_000, tz=UTC)


def now_tid() -> str:
    """A TID for "now", with a random clock identifier as the spec recommends."""
    microseconds = int(datetime.now(UTC).timestamp() * 1_000_000)
    return encode_tid(microseconds, random.randrange(1024))
