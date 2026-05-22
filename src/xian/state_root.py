from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import Any

from contracting import constants as contracting_constants

from xian.utils.encoding import encode_abci_json

STATE_ROOT_VERSION = "xian-state-root-v1"
EMPTY_STATE_ROOT = hashlib.sha256(
    f"{STATE_ROOT_VERSION}:empty".encode("utf-8")
).digest()
LEAF_PREFIX = b"\x00"
NODE_PREFIX = b"\x01"


def is_consensus_state_key(key: str) -> bool:
    return not key.startswith("__") or key.startswith(
        f"__n{contracting_constants.INDEX_SEPARATOR}"
    )


def _length_prefixed(payload: bytes) -> bytes:
    return len(payload).to_bytes(8, "big") + payload


def _hash_leaf(key: str, value: Any) -> bytes:
    key_bytes = key.encode("utf-8")
    value_bytes = encode_abci_json(value)
    return hashlib.sha256(
        LEAF_PREFIX
        + _length_prefixed(key_bytes)
        + _length_prefixed(value_bytes)
    ).digest()


def _hash_node(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(NODE_PREFIX + left + right).digest()


def merkle_root_from_items(items: Iterable[tuple[str, Any]]) -> bytes:
    leaves: list[tuple[str, bytes]] = []
    for key, value in items:
        if not isinstance(key, str):
            raise TypeError("state root keys must be strings")
        if value is None or not is_consensus_state_key(key):
            continue
        leaves.append((key, _hash_leaf(key, value)))

    if not leaves:
        return EMPTY_STATE_ROOT

    leaves.sort(key=lambda item: item[0])
    for index in range(1, len(leaves)):
        if leaves[index - 1][0] == leaves[index][0]:
            raise ValueError(f"duplicate consensus state key: {leaves[index][0]}")

    level = [leaf_hash for _, leaf_hash in leaves]
    while len(level) > 1:
        next_level = []
        for index in range(0, len(level), 2):
            left = level[index]
            right = level[index + 1] if index + 1 < len(level) else left
            next_level.append(_hash_node(left, right))
        level = next_level

    return level[0]


def compute_driver_state_root(driver) -> bytes:
    return merkle_root_from_items(driver.items().items())


def exported_state_items(
    exported_state: dict[str, Any],
) -> list[tuple[str, Any]]:
    items: list[tuple[str, Any]] = [
        (entry["key"], entry["value"])
        for entry in exported_state.get("genesis", [])
    ]
    nonce_prefix = f"__n{contracting_constants.INDEX_SEPARATOR}"
    for nonce in exported_state.get("nonces", []):
        items.append((f"{nonce_prefix}{nonce['key']}", nonce["value"]))
    return items


def compute_exported_state_root(exported_state: dict[str, Any]) -> bytes:
    return merkle_root_from_items(exported_state_items(exported_state))
