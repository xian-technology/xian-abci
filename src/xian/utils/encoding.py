import binascii
import decimal
import hashlib
import json
from datetime import datetime
from typing import Any, Tuple

from loguru import logger
from xian_runtime_types.decimal import ContractingDecimal, canonical_fixed
from xian_runtime_types.encoding import (
    encode_set_value,
    is_wrapper_set,
    loads_wire_json,
    normalize_wire_numbers,
    safe_repr,
)
from xian_runtime_types.set_codec import RESERVED_TAGS
from xian_runtime_types.time import Datetime

from xian.log_safety import error_fields

try:
    from xian_fastpath_core import (
        extract_payload_string as _native_extract_payload_string,
    )
except ImportError:  # pragma: no cover - exercised through fallback path
    _native_extract_payload_string = None

MIN_CANONICAL_JSON_INTEGER = -(2**63)
MAX_CANONICAL_JSON_INTEGER = 2**64 - 1
MAX_CANONICAL_JSON_DEPTH = 128


def _decimal_to_plain_string(value) -> str:
    if isinstance(value, ContractingDecimal):
        value = value._d
    elif not isinstance(value, decimal.Decimal):
        value = decimal.Decimal(str(value))

    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in {"", "-0"}:
        return "0"
    return text


def encode_str(value):
    return value.encode("utf-8")


def canonical_json_value(value: Any, *, _depth: int = 0) -> Any:
    if _depth > MAX_CANONICAL_JSON_DEPTH:
        raise ValueError("recursion limit exceeded")
    if isinstance(value, dict):
        items = []
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("Transaction JSON object keys must be strings")
            items.append((key, canonical_json_value(item, _depth=_depth + 1)))
        return {key: item for key, item in sorted(items)}
    if isinstance(value, list):
        return [canonical_json_value(item, _depth=_depth + 1) for item in value]
    if type(value) is int and not (
        MIN_CANONICAL_JSON_INTEGER <= value <= MAX_CANONICAL_JSON_INTEGER
    ):
        raise ValueError("Transaction bytes are not canonical")
    if isinstance(value, float):
        raise ValueError("Transaction bytes are not canonical")
    return value


def canonical_json_text(value: Any) -> str:
    return json.dumps(
        canonical_json_value(value),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def decode_transaction_bytes(raw) -> Tuple[dict, str]:
    # Returning a Python dict makes decode-only calls boundary-bound; keep the
    # full decode in Python and use native code only for the payload scanner.
    tx_bytes = raw
    tx_hex = tx_bytes.decode("utf-8")
    tx_decoded_bytes = bytes.fromhex(tx_hex)
    tx_str = tx_decoded_bytes.decode("utf-8")
    try:
        tx_json = loads_wire_json(tx_str)
    except ValueError as exc:
        raise ValueError("Transaction bytes are not canonical") from exc

    canonical_tx_str = canonical_json_text(tx_json)
    if tx_str != canonical_tx_str:
        raise ValueError("Transaction bytes are not canonical")
    try:
        payload_str = canonical_json_text(tx_json["payload"])
    except KeyError as exc:
        raise ValueError("Invalid payload") from exc
    return tx_json, payload_str


def encode_transaction_bytes(tx_str: str) -> bytes:
    tx_bytes = tx_str.encode("utf-8")
    tx_hex = binascii.hexlify(tx_bytes).decode("utf-8")
    return tx_hex.encode("utf-8")


def extract_payload_string(json_str):
    if _native_extract_payload_string is not None:
        return _native_extract_payload_string(json_str)

    try:
        # Find the start of the 'payload' object
        start_index = json_str.find('"payload":')
        if start_index == -1:
            raise ValueError("No 'payload' found in the provided JSON string.")

        # Find the opening brace of the 'payload' object
        start_brace_index = json_str.find("{", start_index)
        if start_brace_index == -1:
            raise ValueError("Malformed JSON: No opening brace for 'payload'.")

        # Use a stack to find the matching closing brace, ignoring braces within strings
        brace_count = 0
        in_string = False
        i = start_brace_index
        while i < len(json_str):
            char = json_str[i]

            if char == '"' and not _is_escaped(json_str, i):
                in_string = not in_string

            if not in_string:
                if char == "{":
                    brace_count += 1
                elif char == "}":
                    brace_count -= 1

            # When brace_count is zero, we've found the matching closing brace
            if brace_count == 0:
                return json_str[start_brace_index : i + 1]

            i += 1

        raise ValueError("Malformed JSON: No matching closing brace for 'payload'.")
    except Exception as e:
        logger.bind(**error_fields(e)).error("Transaction encoding failed")
        raise


def _is_escaped(json_str: str, index: int) -> bool:
    slash_count = 0
    cursor = index - 1
    while cursor >= 0 and json_str[cursor] == "\\":
        slash_count += 1
        cursor -= 1
    return slash_count % 2 == 1


def hash_bytes(bytes):
    return hashlib.sha256(bytes).hexdigest()


def convert_binary_to_hex(binary_data):
    try:
        return binascii.hexlify(binary_data).decode()
    except UnicodeDecodeError as e:
        logger.bind(**error_fields(e)).error("Transaction is not valid UTF-8")
        raise
    except Exception as e:
        logger.bind(**error_fields(e)).error("Transaction encoding failed")
        raise


def stringify_decimals(obj):
    try:
        if isinstance(obj, ContractingDecimal):
            return _decimal_to_plain_string(obj)
        elif isinstance(obj, decimal.Decimal):
            return _decimal_to_plain_string(obj)
        elif isinstance(obj, dict):
            return {key: stringify_decimals(val) for key, val in obj.items()}
        elif isinstance(obj, list):
            return [stringify_decimals(elem) for elem in obj]
        elif isinstance(obj, Datetime):
            return str(obj)
        elif isinstance(obj, bytes):
            try:
                return obj.decode("utf-8")
            except UnicodeDecodeError:
                return str(obj)
        else:
            return obj
    except Exception:
        return ""


class _NonStringKey(ValueError):
    pass


def normalize_for_abci_json(obj):
    if is_wrapper_set(obj):
        return normalize_wire_numbers(encode_set_value(obj))
    if isinstance(obj, BaseException):
        return safe_repr(obj) or str(obj)
    if isinstance(obj, (ContractingDecimal, decimal.Decimal)):
        return {"__fixed__": canonical_fixed(obj)}
    if type(obj) in (int, float):
        return normalize_wire_numbers(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Datetime):
        return str(obj)
    if isinstance(obj, bytes):
        try:
            return obj.decode("utf-8")
        except UnicodeDecodeError:
            return str(obj)
    if isinstance(obj, dict):
        if obj.keys() & RESERVED_TAGS:
            return normalize_wire_numbers(obj)
        normalized = []
        for key, value in obj.items():
            if not isinstance(key, str):
                raise _NonStringKey("Non-string key types not allowed.")
            normalized.append((key, normalize_for_abci_json(value)))
        normalized.sort(key=lambda item: item[0])
        return {key: value for key, value in normalized}
    if isinstance(obj, tuple):
        return [normalize_for_abci_json(elem) for elem in obj]
    if isinstance(obj, list):
        return [normalize_for_abci_json(elem) for elem in obj]
    return obj


def normalize_contract_result(value):
    """Return structured receipts for wire values and render local-only shapes as text."""
    try:
        normalized = normalize_for_abci_json(value)
    except _NonStringKey:
        # Contract-local dictionaries can have keys without a JSON representation.
        return safe_repr(value)
    try:
        json.dumps(normalized, allow_nan=False)
    except TypeError:
        return safe_repr(value)
    return normalized


def encode_abci_json(obj) -> bytes:
    normalized = normalize_for_abci_json(obj)
    return json.dumps(normalized, separators=(",", ":")).encode("utf-8")
