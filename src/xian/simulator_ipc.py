from __future__ import annotations

import decimal
import json

from xian_runtime_types.decimal import ContractingDecimal
from xian_runtime_types.time import Datetime

_TYPE_KEY = "__xian_ipc_type__"


def _encode_value(value):
    if isinstance(value, ContractingDecimal):
        return {_TYPE_KEY: "contracting_decimal", "value": str(value)}
    if isinstance(value, decimal.Decimal):
        return {_TYPE_KEY: "decimal", "value": format(value, "f")}
    if isinstance(value, Datetime):
        return {_TYPE_KEY: "datetime", "value": str(value)}
    if isinstance(value, bytes):
        return {_TYPE_KEY: "bytes", "value": value.hex()}
    if isinstance(value, frozenset):
        return {_TYPE_KEY: "frozenset", "items": [_encode_value(item) for item in value]}
    if isinstance(value, set):
        return {_TYPE_KEY: "set", "items": [_encode_value(item) for item in value]}
    if isinstance(value, tuple):
        return {_TYPE_KEY: "tuple", "items": [_encode_value(item) for item in value]}
    if isinstance(value, list):
        return [_encode_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _encode_value(item) for key, item in value.items()}
    return value


def _decode_datetime(value: str) -> Datetime:
    fmt = "%Y-%m-%d %H:%M:%S.%f" if "." in value else "%Y-%m-%d %H:%M:%S"
    return Datetime.strptime(value, fmt)


def _decode_value(value):
    if isinstance(value, list):
        return [_decode_value(item) for item in value]
    if not isinstance(value, dict):
        return value

    value_type = value.get(_TYPE_KEY)
    if value_type == "contracting_decimal" and set(value) == {_TYPE_KEY, "value"}:
        return ContractingDecimal(value["value"])
    if value_type == "decimal" and set(value) == {_TYPE_KEY, "value"}:
        return decimal.Decimal(value["value"])
    if value_type == "datetime" and set(value) == {_TYPE_KEY, "value"}:
        return _decode_datetime(value["value"])
    if value_type == "bytes" and set(value) == {_TYPE_KEY, "value"}:
        return bytes.fromhex(value["value"])
    if value_type == "set" and set(value) == {_TYPE_KEY, "items"}:
        return {_decode_value(item) for item in value["items"]}
    if value_type == "frozenset" and set(value) == {_TYPE_KEY, "items"}:
        return frozenset(_decode_value(item) for item in value["items"])
    if value_type == "tuple" and set(value) == {_TYPE_KEY, "items"}:
        return tuple(_decode_value(item) for item in value["items"])

    return {key: _decode_value(item) for key, item in value.items()}


def dumps(value) -> bytes:
    return json.dumps(_encode_value(value), separators=(",", ":")).encode("utf-8")


def loads(value: bytes) -> object:
    return _decode_value(json.loads(value.decode("utf-8")))
