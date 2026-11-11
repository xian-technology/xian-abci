"""Shared numeric input boundaries for node service configuration."""

import math


def finite_float(value: object, *, name: str) -> float:
    try:
        number = float(value)
    except OverflowError:
        raise ValueError(f"{name} must be finite") from None
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number
