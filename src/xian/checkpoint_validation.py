"""Canonical checkpoint fields shared by recovery and full-store verification."""

import re
from collections.abc import Mapping

from xian.evidence_policy import INT64_MAX, TIMESTAMP_MAX_NS, TIMESTAMP_MIN_NS


def canonical_hex(value: object, label: str, *, length: int = 64) -> str:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{%d}" % length, value) is None:
        raise ValueError(f"{label} must be lowercase {length}-hex")
    return value


def positive_integer(value: object, label: str) -> int:
    if type(value) is not int or not 1 <= value <= INT64_MAX:
        raise ValueError(f"{label} must be a positive int64 integer")
    return value


def canonical_integer(value: object, label: str, *, minimum=1, maximum=INT64_MAX) -> int:
    if type(value) is not str or re.fullmatch(r"0|-?[1-9][0-9]*", value) is None:
        raise ValueError(f"{label} must be a canonical decimal string")
    # Reject excessive input before Python's arbitrary precision conversion.
    if len(value) > 32:
        raise ValueError(f"{label} is outside its supported range")
    number = int(value)
    if not minimum <= number <= maximum:
        raise ValueError(f"{label} is outside its supported range")
    return number


def trusted_checkpoint_point(value: Mapping) -> tuple[int, str, int]:
    # Accept the existing committed /evidence_policy or native-inspector JSON,
    # not an exported state's unrelated number/hash/nanos field shape.
    if (
        not isinstance(value, Mapping)
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
    ):
        raise ValueError("trusted checkpoint requires schema_version 1 query/inspector metadata")
    try:
        return (
            canonical_integer(value["height"], "trusted checkpoint height"),
            canonical_hex(value["app_hash"], "trusted checkpoint app_hash"),
            canonical_integer(
                value["last_block_nanos"],
                "trusted checkpoint nanos",
                minimum=TIMESTAMP_MIN_NS,
                maximum=TIMESTAMP_MAX_NS,
            ),
        )
    except KeyError as error:
        raise ValueError(
            "trusted checkpoint requires height, app_hash and last_block_nanos"
        ) from error
