import json
import shutil
import tempfile
import unittest
from pathlib import Path

from xian.utils.state_patches import StatePatchManager, hash_from_state_changes


class FakeRawDriver:
    def __init__(self):
        self.state = {}
        self.hard_apply_calls = []

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

    def hard_apply(self, nanos):
        self.hard_apply_calls.append(nanos)


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
        self.assertEqual(self.driver.hard_apply_calls, [123456])

    def test_build_applied_patches_for_source_patch_includes_runtime_code(self):
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
        self.assertIn("con_patchable.__code__", change_keys)

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
