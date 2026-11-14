import hashlib
import tempfile
import unittest
from pathlib import Path

from contracting import constants as contracting_constants
from contracting.storage.driver import Driver
from xian_runtime_types.encoding import encode
from xian_runtime_types.time import Datetime, Timedelta

from xian.state_root import (
    EMPTY_STATE_ROOT,
    StateRootCache,
    compute_driver_state_root,
    compute_exported_state_root,
    merkle_root_from_items,
    stored_bytes,
)


def stored_writes(writes):
    return [(key, stored_bytes(value)) for key, value in writes]


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

    def test_timedelta_and_bytearray_leaves_hash_their_stored_bytes(self):
        span, raw = Timedelta(days=-1, seconds=86_399), bytearray(b"ab")
        exported_state = {
            "genesis": [
                {"key": "con_t.span", "value": span},
                {"key": "con_t.raw", "value": raw},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            driver = Driver(vm_profile="xian_vm_v2", storage_home=Path(tmp_dir))
            self.addCleanup(driver.close)
            driver.set("con_t.span", span)
            driver.set("con_t.raw", raw)
            driver.commit()
            root = compute_driver_state_root(driver)

        self.assertEqual(root, compute_exported_state_root(exported_state))
        # Text that merely looks like the value is a different leaf.
        self.assertNotEqual(
            root, merkle_root_from_items([("con_t.span", str(span)), ("con_t.raw", "ab")])
        )

    def test_a_leaf_commits_to_the_stored_value_not_its_text_form(self):
        # Under the ABCI JSON leaf a datetime hashed like its str() and bytes
        # like their UTF-8 text, so different states could share a root.
        pairs = [
            (Datetime(2024, 1, 1), "2024-01-01 00:00:00"),
            (Datetime(2024, 1, 1, 0, 0, 0, 5), "2024-01-01 00:00:00.000005"),
            (b"ok", "ok"),
            ({"at": Datetime(2020, 2, 29)}, {"at": "2020-02-29 00:00:00"}),
            ([b"x"], ["x"]),
        ]
        for typed, text in pairs:
            with self.subTest(typed=typed):
                self.assertNotEqual(
                    merkle_root_from_items([("con_t.v", typed)]),
                    merkle_root_from_items([("con_t.v", text)]),
                )

    def test_the_leaf_hashes_the_key_and_the_stored_bytes(self):
        def framed(payload):
            return len(payload).to_bytes(8, "big") + payload

        key, value = "con_t.v", {"b": Datetime(2024, 1, 1), "a": b"\xff"}
        stored = encode(value).encode()
        self.assertEqual(stored, b'{"b":{"__time__":[2024,1,1,0,0,0,0]},"a":{"__bytes__":"ff"}}')
        empty = hashlib.sha256(b"xian-state-root-v3:empty").digest()
        leaf = hashlib.sha256(
            b"xian-state-root-v3:leaf:" + framed(key.encode()) + framed(stored)
        ).digest()
        root = hashlib.sha256(b"xian-state-root-v3:node:" + empty + leaf + empty).digest()
        self.assertEqual(EMPTY_STATE_ROOT, empty)
        self.assertEqual(merkle_root_from_items([(key, value)]), root)

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
            self.addCleanup(driver.close)
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
            stored_writes(
                [
                    ("currency.balances:alice", 5),
                    ("__local.cache", 99),
                ]
            )
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

        cache.prepare(stored_writes([("currency.balances:alice", 5)]))
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

        prepared_root = cache.prepare(stored_writes([("currency.balances:bob", None)]))

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
                    cache.prepare(stored_writes([("currency.balances:alice", 5), (key, 1)]))
                self.assertEqual(cache.root_hash, original_root)

    def test_prepare_takes_stored_bytes_not_values(self):
        cache = StateRootCache([("currency.balances:alice", 1)])
        with self.assertRaisesRegex(TypeError, "stored bytes"):
            cache.prepare([("currency.balances:alice", 5)])
        self.assertEqual(cache.root_hash, merkle_root_from_items([("currency.balances:alice", 1)]))

    def test_prepared_root_does_not_depend_on_write_order(self):
        base = [(f"con_x.v:{index}", index) for index in range(40)]
        writes = [
            (f"con_x.v:{index}", index * 7 if index % 3 else None) for index in range(0, 60, 2)
        ]
        roots = set()
        for order in (writes, writes[::-1], sorted(writes, key=lambda item: hash(item[0]))):
            cache = StateRootCache(base)
            roots.add(cache.prepare(stored_writes(order)))
            cache.rollback()
            roots_after_rollback = cache.root_hash
            self.assertEqual(roots_after_rollback, StateRootCache(base).root_hash)
        self.assertEqual(len(roots), 1)


if __name__ == "__main__":
    unittest.main()
