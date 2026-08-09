"""net.atmowx.defs#quantity — a decimal as ``value * 10^scale``.

The lexicon data model has no floating point type, and publishers "MUST NOT
report more precision than the sensor resolves", so every value is rounded to a
declared number of decimal places before encoding.
"""

from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, TypedDict


class Quantity(TypedDict, total=False):
    value: int
    scale: int


def to_quantity(value: float, decimals: int) -> Quantity:
    """Encode a decimal at ``decimals`` places of precision.

    Trailing zeros are folded away (1.0 becomes ``{"value": 1}``, not
    ``{"value": 10, "scale": -1}``) and ``scale: 0`` is omitted, matching how
    atmowx publishes its own records.
    """
    if not isinstance(decimals, int) or isinstance(decimals, bool) or not 0 <= decimals <= 9:
        raise ValueError(f"decimals must be an integer in 0..9, got {decimals!r}")
    if not math.isfinite(value):
        raise ValueError(f"not a finite number: {value!r}")

    try:
        # Via str() so the decimal sees the number as written rather than the
        # binary float's full expansion; ROUND_HALF_UP is round-half-away-from-zero.
        scaled = Decimal(str(value)).scaleb(decimals).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    except InvalidOperation as error:
        raise ValueError(f"cannot encode {value!r} at {decimals} decimals: {error}") from error

    significand = int(scaled)
    scale = -decimals

    while scale < 0 and significand != 0 and significand % 10 == 0:
        significand //= 10
        scale += 1
    if significand == 0:
        scale = 0

    if abs(significand) > 2**53 - 1:
        raise ValueError(f"quantity out of safe integer range: {value!r} at {decimals} decimals")

    return {"value": significand} if scale == 0 else {"value": significand, "scale": scale}


def from_quantity(quantity: Any) -> float:
    """Decode a quantity back to a float, for tests and display."""
    if not isinstance(quantity, dict) or "value" not in quantity:
        raise ValueError(f"not a quantity: {quantity!r}")
    return float(quantity["value"]) * 10 ** float(quantity.get("scale", 0))
