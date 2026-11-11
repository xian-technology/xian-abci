from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from xian_runtime_types.encoding import normalize_wire_numbers


def canonical_json_value(value: Any) -> Any:
    return normalize_wire_numbers(value)


def canonical_json_text(value: Any) -> str:
    return json.dumps(canonical_json_value(value), separators=(",", ":"))


def canonical_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
