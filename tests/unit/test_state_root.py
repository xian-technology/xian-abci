import tempfile
import unittest
from pathlib import Path

from contracting import constants as contracting_constants
from contracting.storage.driver import Driver

from xian.state_root import (
    EMPTY_STATE_ROOT,
    StateRootCache,
    compute_driver_state_root,
    compute_exported_state_root,
    merkle_root_from_items,
)


class StateRootTests(unittest.TestCase):
    def test_merkle_root_is_order_independent(self):
        first = merkle_root_from_items(
            [
                ("currency.balances:bob", 2),
                ("currency.balances:alice", 1),
            ]
        )
        second = merkle_root_from_items(
            [
                ("currency.balances:alice", 1),
                ("currency.balances:bob", 2),
            ]
        )

        self.assertEqual(first, second)

    def test_merkle_root_is_independent_of_insertion_order(self):
        items = [(f"currency.balances:{index}", index) for index in range(100)]

        self.assertEqual(
            merkle_root_from_items(items),
            merkle_root_from_items(reversed(items)),
        )

    def test_merkle_root_changes_when_consensus_value_changes(self):
        first = merkle_root_from_items([("currency.balances:alice", 1)])
        second = merkle_root_from_items([("currency.balances:alice", 2)])

        self.assertNotEqual(first, second)

    def test_merkle_root_includes_nonces_but_excludes_local_runtime_keys(self):
        nonce_key = f"__n{contracting_constants.INDEX_SEPARATOR}alice:"
        with_nonce = merkle_root_from_items([(nonce_key, 7)])
        without_nonce = merkle_root_from_items([])
        with_local_only = merkle_root_from_items([("__local.cache", 7)])

        self.assertNotEqual(with_nonce, without_nonce)
        self.assertEqual(with_local_only, EMPTY_STATE_ROOT)

    def test_merkle_root_rejects_duplicate_consensus_keys(self):
        with self.assertRaisesRegex(
            ValueError,
            "duplicate consensus state key",
        ):
            merkle_root_from_items(
                [
                    ("currency.balances:alice", 1),
                    ("currency.balances:alice", 2),
                ]
            )

    def test_timedelta_and_bytearray_leaves_hash_their_wire_form(self):
        from xian_runtime_types.encoding import convert_dict
        from xian_runtime_types.time import Timedelta

        span, raw = Timedelta(days=-1, seconds=86_399), bytearray(b"ab")
        exported_state = {
            "genesis": [
                {"key": "con_t.span", "value": {"__delta__": [-1, 86_399]}},
                {"key": "con_t.raw", "value": {"__bytearray__": "6162"}},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            driver = Driver(vm_profile="xian_vm_v2", storage_home=Path(tmp_dir))
            driver.set("con_t.span", span)
            driver.set("con_t.raw", raw)
            driver.commit()
            root = compute_driver_state_root(driver)

        self.assertEqual(root, compute_exported_state_root(exported_state))
        self.assertEqual(
            root,
            merkle_root_from_items(
                (entry["key"], convert_dict(entry["value"])) for entry in exported_state["genesis"]
            ),
        )
        # Text that merely looks like the value is a different leaf.
        self.assertNotEqual(
            root, merkle_root_from_items([("con_t.span", str(span)), ("con_t.raw", "ab")])
        )

    def test_exported_state_root_matches_driver_root(self):
        nonce_key = f"alice{contracting_constants.DELIMITER}"
        exported_state = {
            "genesis": [
                {"key": "currency.balances:alice", "value": 1},
                {"key": "currency.balances:bob", "value": 2},
            ],
            "nonces": [{"key": nonce_key, "value": 3}],
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            driver = Driver(vm_profile="xian_vm_v2", storage_home=Path(tmp_dir))
            driver.set("currency.balances:alice", 1)
            driver.set("currency.balances:bob", 2)
            driver.set(
                f"__n{contracting_constants.INDEX_SEPARATOR}{nonce_key}",
                3,
            )
            driver.commit()

            self.assertEqual(
                compute_exported_state_root(exported_state),
                compute_driver_state_root(driver),
            )

    def test_state_root_cache_prepare_rollback_and_commit(self):
        items = [
            ("currency.balances:alice", 1),
            ("currency.balances:bob", 2),
        ]
        cache = StateRootCache(items)
        original_root = cache.root_hash

        prepared_root = cache.prepare(
            [
                ("currency.balances:alice", 5),
                ("__local.cache", 99),
            ]
        )
        expected_root = merkle_root_from_items(
            [
                ("currency.balances:alice", 5),
                ("currency.balances:bob", 2),
            ]
        )

        self.assertEqual(prepared_root, expected_root)
        self.assertEqual(cache.root_hash, expected_root)
        cache.rollback()
        self.assertEqual(cache.root_hash, original_root)

        cache.prepare([("currency.balances:alice", 5)])
        cache.commit()
        self.assertEqual(cache.root_hash, expected_root)
        cache.rollback()
        self.assertEqual(cache.root_hash, expected_root)

    def test_state_root_cache_prepare_can_delete_keys(self):
        cache = StateRootCache(
            [
                ("currency.balances:alice", 1),
                ("currency.balances:bob", 2),
            ]
        )

        prepared_root = cache.prepare([("currency.balances:bob", None)])

        self.assertEqual(
            prepared_root,
            merkle_root_from_items([("currency.balances:alice", 1)]),
        )

    def test_state_root_cache_refuses_to_prepare_a_key_lmdb_cannot_store(self):
        # Node-local keys are not hashed but are still written at Commit.
        cache = StateRootCache([("currency.balances:alice", 1)])
        original_root = cache.root_hash
        for key in ("con_demo.data:" + "x" * 498, "__local." + "x" * 504):
            with self.subTest(key=key[:12]):
                with self.assertRaisesRegex(ValueError, "Max is 511"):
                    cache.prepare([("currency.balances:alice", 5), (key, 1)])
                self.assertEqual(cache.root_hash, original_root)

    def test_prepared_root_does_not_depend_on_write_order(self):
        base = [(f"con_x.v:{index}", index) for index in range(40)]
        writes = [
            (f"con_x.v:{index}", index * 7 if index % 3 else None) for index in range(0, 60, 2)
        ]
        roots = set()
        for order in (writes, writes[::-1], sorted(writes, key=lambda item: hash(item[0]))):
            cache = StateRootCache(base)
            roots.add(cache.prepare(order))
            cache.rollback()
            roots_after_rollback = cache.root_hash
            self.assertEqual(roots_after_rollback, StateRootCache(base).root_hash)
        self.assertEqual(len(roots), 1)


if __name__ == "__main__":
    unittest.main()
