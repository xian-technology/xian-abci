import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xian.runtime_features import RuntimeFeatureUnsupportedError
from xian.utils.state_patches import StatePatchManager, hash_from_state_changes


class FakeRawDriver:
    def __init__(self):
        self.state = {}
        self.commits = 0

    def items(self, prefix=""):
        return {
            key: value
            for key, value in self.state.items()
            if key.startswith(prefix)
        }

    def make_key(self, contract, variable, args=None):
        base = f"{contract}.{variable}"
        if args:
            return ":".join([base, *[str(arg) for arg in args]])
        return base

    def get_var(self, contract, variable, arguments=None):
        return self.state.get(self.make_key(contract, variable, arguments))

    def set_var(self, contract, variable, arguments=None, value=None):
        self.state[self.make_key(contract, variable, arguments)] = value

    def set(self, key, value):
        self.state[key] = value

    def commit(self):
        self.commits += 1


class StatePatchManagerTests(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        self.driver = FakeRawDriver()
        self.manager = StatePatchManager(
            self.driver,
            chain_id="test-chain",
        )

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def write_bundle(self, name: str, payload: dict) -> Path:
        path = self.test_dir / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def schedule_bundle(self, *, patch_id: str, bundle_hash: str, height: int):
        contract = "governance"
        self.driver.set_var(
            contract,
            "scheduled_patches",
            [height, patch_id],
            True,
        )
        self.driver.set_var(
            contract, "patches", [patch_id, "status"], "approved"
        )
        self.driver.set_var(contract, "patches", [patch_id, "proposal_id"], 7)
        self.driver.set_var(
            contract, "patches", [patch_id, "bundle_hash"], bundle_hash
        )
        self.driver.set_var(
            contract, "patches", [patch_id, "activation_height"], height
        )
        self.driver.set_var(
            contract, "patches", [patch_id, "summary"], "Apply test patch"
        )
        self.driver.set_var(
            contract, "patches", [patch_id, "uri"], "ipfs://test"
        )
        self.driver.set_var(contract, "patches", [patch_id, "emergency"], False)

    def test_load_bundle_inventory_from_directory(self):
        self.write_bundle(
            "patch-a.json",
            {
                "version": 1,
                "patch_id": "patch-a",
                "activation_height": 12,
                "changes": [
                    {
                        "key": "test_contract.value",
                        "value": "patched",
                        "comment": "update test value",
                    }
                ],
            },
        )
        self.manager.load_patches(self.test_dir)

        self.assertTrue(self.manager.loaded)
        inventory = self.manager.get_local_bundle_inventory()
        self.assertEqual(len(inventory), 1)
        self.assertEqual(inventory[0]["patch_id"], "patch-a")
        self.assertEqual(inventory[0]["activation_height"], 12)

    def test_apply_governed_patch_marks_execution_metadata(self):
        self.write_bundle(
            "patch-a.json",
            {
                "version": 1,
                "patch_id": "patch-a",
                "activation_height": 12,
                "chain_id": "test-chain",
                "changes": [
                    {
                        "key": "test_contract.value",
                        "value": {"nested": 4},
                        "comment": "update test value",
                    }
                ],
            },
        )
        self.manager.load_patches(self.test_dir)
        bundle_hash = self.manager.local_bundles["patch-a"].bundle_hash
        self.schedule_bundle(
            patch_id="patch-a", bundle_hash=bundle_hash, height=12
        )

        patch_hash, executions = self.manager.apply_patches_for_block(
            12,
            123456,
            block_hash="ABC123",
        )

        self.assertIsNotNone(patch_hash)
        self.assertEqual(len(executions), 1)
        self.assertEqual(
            self.driver.state["test_contract.value"],
            {"nested": 4},
        )
        self.assertEqual(
            self.driver.get_var("governance", "patches", ["patch-a", "status"]),
            "applied",
        )
        self.assertEqual(
            self.driver.get_var(
                "governance", "patches", ["patch-a", "applied_block_hash"]
            ),
            "ABC123",
        )
        self.assertEqual(self.driver.commits, 0)

    def test_build_applied_patches_for_source_patch_includes_vm_ir(self):
        self.write_bundle(
            "patch-source.json",
            {
                "version": 1,
                "patch_id": "patch-source",
                "activation_height": 44,
                "changes": [
                    {
                        "key": "con_patchable.__source__",
                        "value": (
                            "value = Variable()\n\n"
                            "@export\n"
                            "def get_value():\n"
                            "    return value.get()\n"
                        ),
                        "comment": "deploy patchable contract",
                    }
                ],
            },
        )
        self.manager.load_patches(self.test_dir)
        bundle_hash = self.manager.local_bundles["patch-source"].bundle_hash
        self.schedule_bundle(
            patch_id="patch-source",
            bundle_hash=bundle_hash,
            height=44,
        )
        self.driver.set_var(
            "governance", "patches", ["patch-source", "status"], "applied"
        )

        patch_hash, executions = self.manager.build_applied_patches_for_block(
            44
        )

        self.assertIsNotNone(patch_hash)
        self.assertEqual(len(executions), 1)
        change_keys = [change["key"] for change in executions[0]["changes"]]
        self.assertIn("con_patchable.__source__", change_keys)
        self.assertNotIn("con_patchable.__code__", change_keys)
        self.assertIn("con_patchable.__xian_ir_v1__", change_keys)

    def test_missing_local_bundle_for_governed_patch_is_an_error(self):
        self.manager.load_patches(self.test_dir)
        self.schedule_bundle(
            patch_id="missing-patch",
            bundle_hash="abc123",
            height=7,
        )

        with self.assertRaises(FileNotFoundError):
            self.manager.apply_patches_for_block(7, 100)

    def test_invalid_bundle_inventory_raises_instead_of_silently_disabling(
        self,
    ):
        self.write_bundle(
            "invalid.json",
            {
                "version": 1,
                "patch_id": "invalid-patch",
                "activation_height": 12,
                "changes": [
                    {
                        "key": "con_invalid.__code__",
                        "value": "bad",
                    }
                ],
            },
        )

        with self.assertRaises(ValueError):
            self.manager.load_patches(self.test_dir)

        self.assertFalse(self.manager.loaded)
        with self.assertRaises(RuntimeError):
            self.manager.get_local_bundle_inventory()

    def test_bundle_cannot_patch_vm_ir_directly(self):
        self.write_bundle(
            "invalid-ir.json",
            {
                "version": 1,
                "patch_id": "invalid-ir-patch",
                "activation_height": 12,
                "changes": [
                    {
                        "key": "con_invalid.__xian_ir_v1__",
                        "value": "{}",
                    }
                ],
            },
        )

        with self.assertRaises(ValueError):
            self.manager.load_patches(self.test_dir)

    def test_bundle_cannot_patch_a_key_lmdb_cannot_store(self):
        # A 512-byte key would be applied and hashed, then refused by LMDB at
        # Commit. A source patch also writes the contract's 4-byte-longer IR key.
        for key in ("con_long.data:" + "x" * 498, "c" * 500 + ".__source__"):
            with self.subTest(key=key[-20:]):
                bundle = self.write_bundle(
                    "long-key.json",
                    {
                        "version": 1,
                        "patch_id": "long-key-patch",
                        "activation_height": 12,
                        "changes": [{"key": key, "value": "@export\ndef f():\n    return 1\n"}],
                    },
                )

                with self.assertRaisesRegex(ValueError, "Max is 511"):
                    self.manager.load_patches(self.test_dir)
                bundle.unlink()

    def fee_policy_bundle(self, value) -> Path:
        return self.write_bundle(
            "fee-policy.json",
            {
                "version": 1,
                "patch_id": "fee-policy-patch",
                "activation_height": 12,
                "changes": [{"key": "__fee_policy", "value": value}],
            },
        )

    def test_bundle_fee_policy_must_be_one_startup_accepts(self):
        # Startup refuses these, so a node that committed one could never start
        # again: the patch would halt the chain for good.
        valid = {"tx_fee_mode": "paid_metered", "max_tx_chi": 200_000, "max_block_chi": 500_000}
        cases = {
            "zero tx cap": {**valid, "max_tx_chi": 0},
            "boolean tx cap": {**valid, "max_tx_chi": True},
            "decimal tx cap": {**valid, "max_tx_chi": {"__fixed__": "200000"}},
            "block cap below tx cap": {**valid, "max_block_chi": 100_000},
            "block cap above limit": {**valid, "max_block_chi": 10_000_001},
            "tx cap above VM ceiling": {**valid, "max_tx_chi": 1_000_001, "max_block_chi": 2_000_000},
            "unsupported mode": {**valid, "tx_fee_mode": "system_unmetered"},
            "non-canonical mode": {**valid, "tx_fee_mode": "PAID_METERED"},
            "missing field": {"tx_fee_mode": "paid_metered", "max_tx_chi": 200_000},
            "unknown field": {**valid, "chi_rate": 20},
            "not a mapping": [valid],
            "null": None,
        }
        for name, value in cases.items():
            with self.subTest(name):
                bundle = self.fee_policy_bundle(value)
                with self.assertRaisesRegex(ValueError, "fee-policy.json"):
                    self.manager.load_patches(self.test_dir)
                self.assertFalse(self.manager.loaded)
                bundle.unlink()

    def test_bundle_may_change_the_fee_policy_to_a_valid_one(self):
        value = {"tx_fee_mode": "free_metered", "max_tx_chi": 100_000, "max_block_chi": 300_000}
        self.fee_policy_bundle(value)
        self.manager.load_patches(self.test_dir)
        bundle = self.manager.local_bundles["fee-policy-patch"]
        self.schedule_bundle(patch_id=bundle.patch_id, bundle_hash=bundle.bundle_hash, height=12)

        self.manager.apply_patches_for_block(12, 123456)

        self.assertEqual(self.driver.state["__fee_policy"], value)

    def runtime_feature_bundle(self, key, value) -> Path:
        return self.write_bundle(
            "runtime-feature.json",
            {
                "version": 1,
                "patch_id": "runtime-feature-patch",
                "activation_height": 12,
                "changes": [{"key": key, "value": value}],
            },
        )

    def chi_rate_bundle(self, value) -> Path:
        return self.write_bundle(
            "chi-rate.json",
            {
                "version": 1,
                "patch_id": "chi-rate-patch",
                "activation_height": 12,
                "changes": [{"key": "chi_cost.S:value", "value": value}],
            },
        )

    def test_bundle_chi_rate_must_be_one_the_node_prices_fees_with(self):
        # Every node prices fees with it from the activation block on and
        # halts on a rate it cannot use (chain_chi_rate).
        cases = {
            "zero": 0,
            "negative": -20,
            "boolean": True,
            "text": "20",
            "decimal": {"__fixed__": "20"},
            "beyond the decimal range": {"__big_int__": str(10**61)},
            "null (deleted)": None,
        }
        for name, value in cases.items():
            with self.subTest(name):
                bundle = self.chi_rate_bundle(value)
                with self.assertRaisesRegex(ValueError, "chi-rate.json.*chi rate"):
                    self.manager.load_patches(self.test_dir)
                self.assertFalse(self.manager.loaded)
                bundle.unlink()

    def test_bundle_may_change_the_chi_rate_to_a_valid_one(self):
        self.chi_rate_bundle(2000)
        self.manager.load_patches(self.test_dir)
        bundle = self.manager.local_bundles["chi-rate-patch"]
        self.schedule_bundle(patch_id=bundle.patch_id, bundle_hash=bundle.bundle_hash, height=12)

        self.manager.apply_patches_for_block(12, 123456)

        self.assertEqual(self.driver.state["chi_cost.S:value"], 2000)

    def test_bundle_runtime_feature_must_be_one_genesis_writes(self):
        # Genesis writes a supported feature as a JSON boolean; anything else
        # would be read differently (or refused) by running and restarted nodes.
        cases = {
            "unknown feature": ("__runtime_features.unknown", True),
            "empty feature": ("__runtime_features.", True),
            "nested key": ("__runtime_features.zk.extra", True),
            "string value": ("__runtime_features.zk", "true"),
            "integer value": ("__runtime_features.zk", 1),
            "mapping value": ("__runtime_features.zk", {"enabled": True}),
            "null": ("__runtime_features.zk", None),
        }
        for name, (key, value) in cases.items():
            with self.subTest(name):
                bundle = self.runtime_feature_bundle(key, value)
                with self.assertRaisesRegex(ValueError, "runtime-feature.json"):
                    self.manager.load_patches(self.test_dir)
                self.assertFalse(self.manager.loaded)
                bundle.unlink()

    def apply_runtime_feature_patch(self, value: bool, *, verifier_available: bool):
        self.runtime_feature_bundle("__runtime_features.zk", value)
        self.manager.load_patches(self.test_dir)
        bundle = self.manager.local_bundles["runtime-feature-patch"]
        self.schedule_bundle(patch_id=bundle.patch_id, bundle_hash=bundle.bundle_hash, height=12)
        with patch("contracting.stdlib.bridge.zk.is_available", return_value=verifier_available):
            self.manager.apply_patches_for_block(12, 123456)

    def test_bundle_may_switch_zk_on_and_off(self):
        for value in (True, False):
            with self.subTest(value=value):
                self.driver.state.clear()
                self.apply_runtime_feature_patch(value, verifier_available=True)
                self.assertIs(self.driver.state["__runtime_features.zk"], value)

    def test_a_node_without_the_zk_verifier_refuses_to_activate_zk(self):
        # Applying it would commit a chain state this node cannot execute.
        with self.assertRaisesRegex(RuntimeFeatureUnsupportedError, "zk verifier"):
            self.apply_runtime_feature_patch(True, verifier_available=False)
        self.assertNotIn("__runtime_features.zk", self.driver.state)
        self.assertEqual(
            self.driver.get_var("governance", "patches", ["runtime-feature-patch", "status"]),
            "approved",
        )

    def test_a_node_without_the_zk_verifier_may_deactivate_zk(self):
        self.apply_runtime_feature_patch(False, verifier_available=False)
        self.assertIs(self.driver.state["__runtime_features.zk"], False)

    def test_bundle_hash_mismatch_is_a_hard_error(self):
        self.write_bundle(
            "patch-a.json",
            {
                "version": 1,
                "patch_id": "patch-a",
                "activation_height": 12,
                "chain_id": "test-chain",
                "changes": [
                    {
                        "key": "test_contract.value",
                        "value": "patched",
                    }
                ],
            },
        )
        self.manager.load_patches(self.test_dir)
        self.schedule_bundle(
            patch_id="patch-a",
            bundle_hash="wrong-hash",
            height=12,
        )

        with self.assertRaises(ValueError):
            self.manager.apply_patches_for_block(12, 123456)

    def test_hash_from_state_changes_ignores_comments(self):
        hash_one = hash_from_state_changes(
            [{"key": "a", "value": 1, "comment": "first"}]
        )
        hash_two = hash_from_state_changes(
            [{"key": "a", "value": 1, "comment": "second"}]
        )
        self.assertEqual(hash_one, hash_two)


if __name__ == "__main__":
    unittest.main()
