from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from contracting import constants as contracting_constants
from contracting.runtime_features import RUNTIME_FEATURE_STATE_PREFIX
from contracting.storage.lmdb_environment import validate_storage_key
from xian_runtime_types.encoding import decode
from xian_vm_core import StateRootTree, state_root_leaf_hash

from xian.execution_policy import EXECUTION_POLICY_KEY
from xian.fee_policy import FEE_POLICY_KEY
from xian.utils.encoding import encode_abci_json

# The fee policy and runtime features change execution results, so they are
# hashed like contract state; other double-underscore keys are node-local.
HASHED_NODE_KEYS = (EXECUTION_POLICY_KEY, FEE_POLICY_KEY)
HASHED_NODE_PREFIXES = (
    f"__n{contracting_constants.INDEX_SEPARATOR}",
    RUNTIME_FEATURE_STATE_PREFIX,
)
EMPTY_STATE_ROOT = StateRootTree().root_hash


def is_consensus_state_key(key: str) -> bool:
    return (
        not key.startswith("__") or key in HASHED_NODE_KEYS or key.startswith(HASHED_NODE_PREFIXES)
    )


def _hash_leaf(key: str, value: Any) -> bytes:
    return state_root_leaf_hash(key, encode_abci_json(value))


class StateRootCache:
    """The consensus state root (``xian_vm_core.StateRootTree``, defined in
    vm-core's ``state_root.rs``) and the leaves staged for the current block."""

    def __init__(self, items: Iterable[tuple[str, Any]] = ()):
        self._tree = StateRootTree()
        self._staged_old_leaf_hashes: dict[str, bytes | None] = {}
        self.rebuild(items)

    @classmethod
    def from_driver(cls, driver) -> "StateRootCache":
        cache = cls()
        cache.rebuild_from_driver(driver)
        return cache

    @property
    def root_hash(self) -> bytes:
        return self._tree.root_hash

    @property
    def leaf_count(self) -> int:
        return len(self._tree)

    def rebuild(self, items: Iterable[tuple[str, Any]]) -> bytes:
        self.rollback()
        leaves = []
        for key, value in items:
            if not isinstance(key, str):
                raise TypeError("state root keys must be strings")
            if value is None or not is_consensus_state_key(key):
                continue
            leaves.append((key, _hash_leaf(key, value)))
        self._tree = StateRootTree.from_leaves(leaves)
        return self.root_hash

    def rebuild_from_driver(self, driver) -> bytes:
        """Rebuild from the driver's merged state, as ``rebuild(driver.items())``.

        Committed V2 storage is read in Rust; entries only Python can settle
        (see ``StateRootTree.from_storage``) are decoded here, then the
        driver's pending writes are applied on top.
        """
        storage = driver.committed_storage()
        if storage is None:
            return self.rebuild(driver.items().items())
        self.rollback()
        tree, handed_back = StateRootTree.from_storage(
            storage[0], list(HASHED_NODE_KEYS), list(HASHED_NODE_PREFIXES)
        )
        pending = dict(driver.pending_items())
        for raw_key, stored in handed_back:
            key = raw_key.decode("utf-8")
            value = decode(stored, vm_profile=driver.vm_profile)
            if key not in pending and value is not None and is_consensus_state_key(key):
                tree.set_leaf(key, _hash_leaf(key, value))
        self._tree = tree
        for key, value in pending.items():
            if is_consensus_state_key(key):
                self._set_leaf_hash(key, None if value is None else _hash_leaf(key, value))
        return self.root_hash

    def prepare(self, writes: Iterable[tuple[str, Any]]) -> bytes:
        """Stage pending ``(key, value)`` writes (None deletes); any order."""
        self.rollback()
        writes = list(writes)
        # Commit persists every pending key, hashed or node-local. A key LMDB
        # cannot store is refused here, before the block has a state root.
        for key, _ in writes:
            if not isinstance(key, str):
                raise TypeError("state root keys must be strings")
            validate_storage_key(key)
        for key, value in writes:
            if not is_consensus_state_key(key):
                continue
            new_leaf_hash = None if value is None else _hash_leaf(key, value)
            old_leaf_hash = self._tree.leaf_hash(key)
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
        for key, old_leaf_hash in reversed(list(self._staged_old_leaf_hashes.items())):
            self._set_leaf_hash(key, old_leaf_hash)
        self._staged_old_leaf_hashes.clear()

    def _set_leaf_hash(self, key: str, leaf_hash: bytes | None) -> None:
        if leaf_hash is None:
            self._tree.remove(key)
        else:
            self._tree.set_leaf(key, leaf_hash)


def merkle_root_from_items(items: Iterable[tuple[str, Any]]) -> bytes:
    return StateRootCache(items).root_hash


def compute_driver_state_root(driver) -> bytes:
    return StateRootCache.from_driver(driver).root_hash


def exported_state_items(
    exported_state: dict[str, Any],
) -> list[tuple[str, Any]]:
    items: list[tuple[str, Any]] = [
        (entry["key"], entry["value"]) for entry in exported_state.get("genesis", [])
    ]
    nonce_prefix = f"__n{contracting_constants.INDEX_SEPARATOR}"
    for nonce in exported_state.get("nonces", []):
        items.append((f"{nonce_prefix}{nonce['key']}", nonce["value"]))
    return items


def compute_exported_state_root(exported_state: dict[str, Any]) -> bytes:
    return merkle_root_from_items(exported_state_items(exported_state))
