import tempfile
import unittest
from pathlib import Path

from contracting import constants as contracting_constants
from contracting.storage.driver import Driver

from xian.state_root import (
    EMPTY_STATE_ROOT,
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
            driver = Driver(storage_home=Path(tmp_dir))
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


if __name__ == "__main__":
    unittest.main()
