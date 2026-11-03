"""Bound operational records before formatting, queueing, or serialization."""

from __future__ import annotations

import json
import math
import unicodedata
from datetime import UTC
from itertools import islice
from pathlib import Path

MAX_MESSAGE = 2048
MAX_VALUE = 256
MAX_FIELDS = 24
_SENSITIVE = frozenset(
    {
        "password",
        "passphrase",
        "secret",
        "private_key",
        "signing_key",
        "private",
        "credential",
        "mnemonic",
        "seed",
        "authorization",
        "token",
        "api_key",
        "signature",
        "payload",
        "kwargs",
        "raw_tx",
        "transaction",
        "state",
        "result",
        "headers",
        "url",
    }
)
_CONTEXT_ORDER = (
    "stage",
    "block_height",
    "block_hash",
    "tx_index",
    "tx_hash",
    "raw_tx_hash",
    "sender",
    "contract",
    "function",
    "nonce",
    "status",
)


def safe_text(value: str, limit: int) -> str:
    """Bound before escaping; never invoke arbitrary object repr methods."""
    pieces = []
    for char in value[:limit]:
        if unicodedata.category(char).startswith("C") or char in "\u2028\u2029":
            pieces.append(char.encode("unicode_escape").decode("ascii"))
        else:
            pieces.append(char)
    text = "".join(pieces)
    return text[:limit] + ("…" if len(value) > limit or len(text) > limit else "")


def safe_value(value):
    if isinstance(value, str):
        return safe_text(value, MAX_VALUE)
    if value is None or isinstance(value, bool):
        return value
    if type(value) is int:
        return value if value.bit_length() <= 64 else "<large integer>"
    if type(value) is float:
        return value if math.isfinite(value) else str(value)
    return "<" + safe_text(type(value).__name__, 64) + ">"


def _field_candidates(fields: dict):
    priority = ("error_type", "errno", "stack", "service", "pid", *_CONTEXT_ORDER)
    for key in priority:
        if key in fields:
            yield key, fields[key]
    for key, value in islice(fields.items(), MAX_FIELDS):
        if key not in priority:
            yield key, value


def _sensitive_field(key: str, value) -> bool:
    # Counts and byte lengths convey volume, never payload contents.
    if type(value) is int and key.endswith(("_count", "_bytes")):
        return False
    normalized = key.lower().replace("-", "_")
    return normalized in _SENSITIVE or bool(set(normalized.split("_")) & _SENSITIVE)


def clean_fields(fields: dict) -> dict:
    result = {}
    for key, value in islice(_field_candidates(fields), MAX_FIELDS):
        if not isinstance(key, str) or key == "context" or value is None:
            continue
        key = safe_text(key, 64)
        result[key] = "[redacted]" if _sensitive_field(key, value) else safe_value(value)
    return result


def context_text(fields: dict) -> str:
    fields = clean_fields(fields)
    keys = [key for key in _CONTEXT_ORDER if key in fields]
    keys.extend(sorted(fields.keys() - set(keys)))
    return " | " + " ".join(f"{key}={_display(fields[key])}" for key in keys) if keys else ""


def _display(value) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, str) and any(char.isspace() or char in '=|"' for char in value):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _exception_fields(exception) -> dict:
    result = {"error_type": safe_text(exception.type.__name__, 64)}
    errno = getattr(exception.value, "errno", None)
    if type(errno) is int:
        result["errno"] = errno
    locations = []
    trace = exception.traceback
    # Keep locations only; no source lines, locals, paths, or exception values.
    for _ in range(32):
        if trace is None:
            break
        code = trace.tb_frame.f_code
        locations.append(f"{Path(code.co_filename).name}:{trace.tb_lineno}:{code.co_name}")
        trace = trace.tb_next
    prefix = "[truncated after 32 frames] " if trace is not None else ""
    result["stack"] = safe_text(prefix + " > ".join(locations[-6:]), MAX_VALUE)
    return result


def sanitize_record(record: dict) -> None:
    fields = clean_fields(record["extra"])
    if record["exception"] is not None:
        fields.update(_exception_fields(record["exception"]))
        record["exception"] = None
    fields["service"] = "xian-abci"
    fields["pid"] = record["process"].id
    record["message"] = safe_text(record["message"], MAX_MESSAGE)
    record["extra"] = clean_fields(fields)
    record["extra"]["context"] = context_text(record["extra"])
    record["time"] = record["time"].astimezone(UTC)


_SAFE_REASONS = {
    "Bad signature": "invalid_signature",
    "Wrong chain_id": "wrong_chain_id",
    "Transaction sender has too few chi for this transaction": "insufficient_chi",
    "Transaction has unexpected or missing keys": "invalid_transaction_keys",
    "Transaction has wrongly formatted dictionary": "invalid_transaction_shape",
    "Metadata is missing": "missing_metadata",
    "Wrong number of metadata entries": "invalid_metadata",
    "Payload is missing": "missing_payload",
    "Payload keys are not valid": "invalid_payload_keys",
    "Transaction contract name is invalid": "invalid_contract_name",
}


def error_fields(error: Exception) -> dict:
    """Expose only type, errno, and exact trusted rejection classifications."""
    fields = {"error_type": type(error).__name__}
    errno = getattr(error, "errno", None)
    if type(errno) is int:
        fields["errno"] = errno
    message = error.args[0] if error.args else None
    if type(message) is str and len(message) <= 256 and message in _SAFE_REASONS:
        fields["reason_code"] = _SAFE_REASONS[message]
    return fields
