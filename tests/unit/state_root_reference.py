"""Reference state root: the pure-Python treap the node used before the Rust tree.

Frozen copy of the consensus definition (``xian-state-root-v2``), kept only as a
test oracle: the node's root must equal what this computes over the same state.
It decodes every stored value in Python (``Driver.items``) and hashes the ABCI
JSON of each consensus value into a treap keyed by key, prioritised by hash.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from contracting import constants as contracting_constants
from contracting.runtime_features import RUNTIME_FEATURE_STATE_PREFIX

from xian.execution_policy import EXECUTION_POLICY_KEY
from xian.fee_policy import FEE_POLICY_KEY
from xian.utils.encoding import encode_abci_json

VERSION = "xian-state-root-v2"
EMPTY = hashlib.sha256(f"{VERSION}:empty".encode()).digest()
LEAF = f"{VERSION}:leaf:".encode()
NODE = f"{VERSION}:node:".encode()
PRIORITY = f"{VERSION}:priority:".encode()


def is_hashed(key: str) -> bool:
    return (
        key == EXECUTION_POLICY_KEY
        or not key.startswith("__")
        or key.startswith(f"__n{contracting_constants.INDEX_SEPARATOR}")
        or key == FEE_POLICY_KEY
        or key.startswith(RUNTIME_FEATURE_STATE_PREFIX)
    )


def _framed(payload: bytes) -> bytes:
    return len(payload).to_bytes(8, "big") + payload


def leaf_hash(key: str, value) -> bytes:
    return hashlib.sha256(
        LEAF + _framed(key.encode()) + _framed(encode_abci_json(value))
    ).digest()


@dataclass(slots=True)
class _Node:
    key: str
    priority: int
    leaf: bytes
    left: _Node | None = None
    right: _Node | None = None
    hash: bytes = b""


def _hash(node: _Node | None) -> bytes:
    return EMPTY if node is None else node.hash


def _refresh(node: _Node) -> _Node:
    node.hash = hashlib.sha256(NODE + _hash(node.left) + node.leaf + _hash(node.right)).digest()
    return node


def _insert(node: _Node | None, key: str, leaf: bytes) -> _Node:
    if node is None:
        priority = int.from_bytes(hashlib.sha256(PRIORITY + key.encode()).digest(), "big")
        return _refresh(_Node(key, priority, leaf))
    if key == node.key:
        node.leaf = leaf
    elif key < node.key:
        node.left = _insert(node.left, key, leaf)
        if node.left.priority < node.priority:
            child, node.left = node.left, node.left.right
            child.right = _refresh(node)
            return _refresh(child)
    else:
        node.right = _insert(node.right, key, leaf)
        if node.right.priority < node.priority:
            child, node.right = node.right, node.right.left
            child.left = _refresh(node)
            return _refresh(child)
    return _refresh(node)


def root_of_leaves(leaves) -> bytes:
    root = None
    for key, leaf in leaves:
        root = _insert(root, key, leaf)
    return _hash(root)


def consensus_leaves(items) -> list[tuple[str, bytes]]:
    return [
        (key, leaf_hash(key, value))
        for key, value in items
        if value is not None and is_hashed(key)
    ]


def root_of_items(items) -> bytes:
    return root_of_leaves(consensus_leaves(items))


def driver_root(driver) -> tuple[bytes, int]:
    """(root, leaf count) of the driver's merged state, decoded in Python."""
    leaves = consensus_leaves(driver.items().items())
    return root_of_leaves(leaves), len(leaves)
