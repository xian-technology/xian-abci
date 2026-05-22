from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from contracting import constants as contracting_constants

from xian.utils.encoding import encode_abci_json

STATE_ROOT_VERSION = "xian-state-root-v2"
EMPTY_STATE_ROOT = hashlib.sha256(
    f"{STATE_ROOT_VERSION}:empty".encode("utf-8")
).digest()
LEAF_PREFIX = f"{STATE_ROOT_VERSION}:leaf:".encode("utf-8")
NODE_PREFIX = f"{STATE_ROOT_VERSION}:node:".encode("utf-8")
PRIORITY_PREFIX = f"{STATE_ROOT_VERSION}:priority:".encode("utf-8")


def is_consensus_state_key(key: str) -> bool:
    return not key.startswith("__") or key.startswith(
        f"__n{contracting_constants.INDEX_SEPARATOR}"
    )


def _length_prefixed(payload: bytes) -> bytes:
    return len(payload).to_bytes(8, "big") + payload


def _hash_priority(key: str) -> int:
    digest = hashlib.sha256(PRIORITY_PREFIX + key.encode("utf-8")).digest()
    return int.from_bytes(digest, "big")


def _hash_leaf(key: str, value: Any) -> bytes:
    key_bytes = key.encode("utf-8")
    value_bytes = encode_abci_json(value)
    return hashlib.sha256(
        LEAF_PREFIX
        + _length_prefixed(key_bytes)
        + _length_prefixed(value_bytes)
    ).digest()


def _hash_node(left: bytes, leaf_hash: bytes, right: bytes) -> bytes:
    return hashlib.sha256(NODE_PREFIX + left + leaf_hash + right).digest()


@dataclass(slots=True)
class _TreapNode:
    key: str
    priority: int
    leaf_hash: bytes
    left: "_TreapNode | None" = None
    right: "_TreapNode | None" = None
    root_hash: bytes = b""


def _subtree_hash(node: _TreapNode | None) -> bytes:
    return EMPTY_STATE_ROOT if node is None else node.root_hash


def _refresh(node: _TreapNode) -> _TreapNode:
    node.root_hash = _hash_node(
        _subtree_hash(node.left),
        node.leaf_hash,
        _subtree_hash(node.right),
    )
    return node


def _new_node(key: str, leaf_hash: bytes) -> _TreapNode:
    return _refresh(
        _TreapNode(
            key=key,
            priority=_hash_priority(key),
            leaf_hash=leaf_hash,
        )
    )


def _rotate_right(node: _TreapNode) -> _TreapNode:
    left = node.left
    assert left is not None
    node.left = left.right
    left.right = _refresh(node)
    return _refresh(left)


def _rotate_left(node: _TreapNode) -> _TreapNode:
    right = node.right
    assert right is not None
    node.right = right.left
    right.left = _refresh(node)
    return _refresh(right)


def _insert_or_update(
    node: _TreapNode | None,
    key: str,
    leaf_hash: bytes,
) -> _TreapNode:
    if node is None:
        return _new_node(key, leaf_hash)
    if key == node.key:
        node.leaf_hash = leaf_hash
        return _refresh(node)
    if key < node.key:
        node.left = _insert_or_update(node.left, key, leaf_hash)
        if node.left is not None and node.left.priority < node.priority:
            return _rotate_right(node)
        return _refresh(node)

    node.right = _insert_or_update(node.right, key, leaf_hash)
    if node.right is not None and node.right.priority < node.priority:
        return _rotate_left(node)
    return _refresh(node)


def _merge(
    left: _TreapNode | None,
    right: _TreapNode | None,
) -> _TreapNode | None:
    if left is None:
        return right
    if right is None:
        return left
    if left.priority < right.priority:
        left.right = _merge(left.right, right)
        return _refresh(left)
    right.left = _merge(left, right.left)
    return _refresh(right)


def _delete(node: _TreapNode | None, key: str) -> _TreapNode | None:
    if node is None:
        return None
    if key == node.key:
        return _merge(node.left, node.right)
    if key < node.key:
        node.left = _delete(node.left, key)
        return _refresh(node)
    node.right = _delete(node.right, key)
    return _refresh(node)


class StateRootCache:
    def __init__(self, items: Iterable[tuple[str, Any]] = ()):
        self._root: _TreapNode | None = None
        self._leaf_hashes: dict[str, bytes] = {}
        self._staged_old_leaf_hashes: dict[str, bytes | None] = {}
        self.rebuild(items)

    @classmethod
    def from_driver(cls, driver) -> "StateRootCache":
        return cls(driver.items().items())

    @property
    def root_hash(self) -> bytes:
        return _subtree_hash(self._root)

    @property
    def leaf_count(self) -> int:
        return len(self._leaf_hashes)

    def rebuild(self, items: Iterable[tuple[str, Any]]) -> bytes:
        self.rollback()
        self._root = None
        self._leaf_hashes = {}
        for key, value in items:
            if not isinstance(key, str):
                raise TypeError("state root keys must be strings")
            if value is None or not is_consensus_state_key(key):
                continue
            if key in self._leaf_hashes:
                raise ValueError(f"duplicate consensus state key: {key}")
            self._set_leaf_hash(key, _hash_leaf(key, value))
        return self.root_hash

    def prepare(self, writes: dict[str, Any]) -> bytes:
        self.rollback()
        for key, value in writes.items():
            if not isinstance(key, str):
                raise TypeError("state root keys must be strings")
            if not is_consensus_state_key(key):
                continue
            new_leaf_hash = None if value is None else _hash_leaf(key, value)
            old_leaf_hash = self._leaf_hashes.get(key)
            if old_leaf_hash == new_leaf_hash:
                continue
            self._staged_old_leaf_hashes[key] = old_leaf_hash
            self._set_leaf_hash(key, new_leaf_hash)
        return self.root_hash

    def commit(self) -> None:
        self._staged_old_leaf_hashes.clear()

    def rollback(self) -> None:
        if not self._staged_old_leaf_hashes:
            return
        for key, old_leaf_hash in reversed(
            list(self._staged_old_leaf_hashes.items())
        ):
            self._set_leaf_hash(key, old_leaf_hash)
        self._staged_old_leaf_hashes.clear()

    def _set_leaf_hash(self, key: str, leaf_hash: bytes | None) -> None:
        if leaf_hash is None:
            self._leaf_hashes.pop(key, None)
            self._root = _delete(self._root, key)
            return
        self._leaf_hashes[key] = leaf_hash
        self._root = _insert_or_update(self._root, key, leaf_hash)


def merkle_root_from_items(items: Iterable[tuple[str, Any]]) -> bytes:
    return StateRootCache(items).root_hash


def compute_driver_state_root(driver) -> bytes:
    return StateRootCache.from_driver(driver).root_hash


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
