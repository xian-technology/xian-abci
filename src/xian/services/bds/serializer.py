from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from xian_runtime_types.encoding import encode as encode_runtime_value


def canonical_json_value(value: Any) -> Any:
    return json.loads(encode_runtime_value(value))


def canonical_json_text(value: Any) -> str:
    return json.dumps(canonical_json_value(value), separators=(",", ":"))


def canonical_result_value(value: Any) -> Any:
    if value is None or value == "None":
        return None
    return canonical_json_value(value)


def canonical_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
