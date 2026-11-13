"""Reference state root (``xian-state-root-v3``) in plain Python, as a test oracle.

A leaf commits to a hashed key and the exact bytes LMDB stores for it (the V2
stored encoding the Python and Rust encoders write byte-identically). Leaves
sit in a treap keyed by key and prioritised by a hash of the key; each node
hashes its left subtree, its leaf and its right subtree. A stored JSON
``null`` reads as an absent key. The node's root must equal this one.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from contracting import constants as contracting_constants
from contracting.runtime_features import RUNTIME_FEATURE_STATE_PREFIX
from xian_runtime_types.encoding import encode

from xian.execution_policy import EXECUTION_POLICY_KEY
from xian.fee_policy import FEE_POLICY_KEY

VERSION = "xian-state-root-v3"
EMPTY = hashlib.sha256(f"{VERSION}:empty".encode()).digest()
LEAF = f"{VERSION}:leaf:".encode()
NODE = f"{VERSION}:node:".encode()
PRIORITY = f"{VERSION}:priority:".encode()
ABSENT = (None, b"null")


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


def stored(value) -> bytes:
    return encode(value).encode()


def leaf_hash(key: str, stored_bytes: bytes) -> bytes:
    return hashlib.sha256(LEAF + _framed(key.encode()) + _framed(stored_bytes)).digest()


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


def root_of_stored(entries) -> tuple[bytes, int]:
    """(root, leaf count) of ``(key, stored bytes or None)`` entries."""
    root, count = None, 0
    for key, raw in entries:
        if raw not in ABSENT and is_hashed(key):
            root = _insert(root, key, leaf_hash(key, raw))
            count += 1
    return _hash(root), count


def root_of_items(items) -> bytes:
    """Root of ``(key, Python value)`` items, stored with the V2 encoder."""
    entries = [(key, None if value is None else stored(value)) for key, value in items]
    return root_of_stored(entries)[0]


def driver_root(driver) -> tuple[bytes, int]:
    """(root, leaf count) of the driver's committed bytes with its pending bytes on top."""
    environment = driver.committed_storage()[0]
    merged = {key.decode("utf-8"): raw for key, raw in environment.items(b"")}
    merged.update(driver.pending_stored_items())
    return root_of_stored(sorted(merged.items()))
