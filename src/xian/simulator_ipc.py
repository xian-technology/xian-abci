from __future__ import annotations

import decimal
import json

from xian_runtime_types.collections import ContractingFrozenSet, ContractingSet
from xian_runtime_types.decimal import ContractingDecimal
from xian_runtime_types.time import Datetime

_TYPE_KEY = "__xian_ipc_type__"
_SCALAR_TYPES = (
    (ContractingDecimal, "contracting_decimal", str),
    (decimal.Decimal, "decimal", str),
    (Datetime, "datetime", str),
    (bytes, "bytes", bytes.hex),
)
_COLLECTION_TYPES = (
    (decimal.DecimalTuple, "decimal_tuple"),
    (ContractingFrozenSet, "contracting_frozenset"),
    (ContractingSet, "contracting_set"),
    (frozenset, "frozenset"),
    (set, "set"),
    (tuple, "tuple"),
)


def _encode_value(value):
    for value_type, tag, encode in _SCALAR_TYPES:
        if isinstance(value, value_type):
            return {_TYPE_KEY: tag, "value": encode(value)}
    for value_type, tag in _COLLECTION_TYPES:
        if isinstance(value, value_type):
            return {_TYPE_KEY: tag, "items": [_encode_value(item) for item in value]}
    if isinstance(value, list):
        return [_encode_value(item) for item in value]
    if isinstance(value, dict):
        return _encode_mapping(value)
    return value


def _encode_mapping(value):
    # Preserve ordinary worker envelopes while escaping marker-shaped user data
    # and keys that JSON would reject or silently coerce to strings.
    if _TYPE_KEY not in value and all(isinstance(key, str) for key in value):
        return {key: _encode_value(item) for key, item in value.items()}
    return {
        _TYPE_KEY: "dict",
        "items": [[_encode_value(key), _encode_value(item)] for key, item in value.items()],
    }


def _decode_datetime(value: str) -> Datetime:
    fmt = "%Y-%m-%d %H:%M:%S.%f" if "." in value else "%Y-%m-%d %H:%M:%S"
    return Datetime.strptime(value, fmt)


_SCALAR_DECODERS = {
    "contracting_decimal": ContractingDecimal,
    "decimal": decimal.Decimal,
    "datetime": _decode_datetime,
    "bytes": bytes.fromhex,
}
_COLLECTION_DECODERS = {tag: value_type for value_type, tag in _COLLECTION_TYPES}
_COLLECTION_DECODERS.update(
    decimal_tuple=lambda items: decimal.DecimalTuple(*items),
    dict=dict,
)


def _decode_value(value):
    if isinstance(value, list):
        return [_decode_value(item) for item in value]
    if not isinstance(value, dict):
        return value
    tag = value.get(_TYPE_KEY)
    if isinstance(tag, str):
        if tag in _SCALAR_DECODERS and set(value) == {_TYPE_KEY, "value"}:
            return _SCALAR_DECODERS[tag](value["value"])
        if tag in _COLLECTION_DECODERS and set(value) == {_TYPE_KEY, "items"}:
            return _COLLECTION_DECODERS[tag](_decode_value(item) for item in value["items"])
    return {key: _decode_value(item) for key, item in value.items()}


def dumps(value) -> bytes:
    return json.dumps(_encode_value(value), separators=(",", ":")).encode("utf-8")


def loads(value: bytes) -> object:
    return _decode_value(json.loads(value.decode("utf-8")))
