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
from xian_runtime_types.time import Datetime, Timedelta

from xian.log_safety import error_fields

MIN_CANONICAL_JSON_INTEGER = -(2**63)
MAX_CANONICAL_JSON_INTEGER = 2**64 - 1
MAX_CANONICAL_JSON_DEPTH = 128


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
        if isinstance(obj, (ContractingDecimal, decimal.Decimal)):
            return canonical_fixed(obj)
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


def _abci_bytes(obj):
    try:
        return obj.decode("utf-8")
    except UnicodeDecodeError:
        return str(obj)


def _abci_dict(obj):
    if obj.keys() & RESERVED_TAGS:
        return normalize_wire_numbers(obj)
    normalized = []
    for key, value in obj.items():
        if not isinstance(key, str):
            raise _NonStringKey("Non-string key types not allowed.")
        normalized.append((key, normalize_for_abci_json(value)))
    normalized.sort(key=lambda item: item[0])
    return dict(normalized)


def _abci_sequence(obj):
    return [normalize_for_abci_json(elem) for elem in obj]


# The JSON form of each runtime type in receipts (state-root leaves hash the
# stored bytes instead); the first matching entry applies. Timedeltas and bytearrays keep the tagged wire
# form the encoder writes.
_ABCI_JSON_FORMS = (
    (BaseException, lambda obj: safe_repr(obj) or str(obj)),
    ((ContractingDecimal, decimal.Decimal), lambda obj: {"__fixed__": canonical_fixed(obj)}),
    ((Timedelta, bytearray), normalize_wire_numbers),
    (datetime, datetime.isoformat),
    (Datetime, str),
    (bytes, _abci_bytes),
    (dict, _abci_dict),
    ((tuple, list), _abci_sequence),
)


def normalize_for_abci_json(obj):
    if is_wrapper_set(obj):
        return normalize_wire_numbers(encode_set_value(obj))
    if type(obj) in (int, float):
        return normalize_wire_numbers(obj)
    for types, render in _ABCI_JSON_FORMS:
        if isinstance(obj, types):
            return render(obj)
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
