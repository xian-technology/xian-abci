import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xian.execution_policy import EXECUTION_POLICY_KEY, current_execution_policy
from xian.state_export import (
    build_exported_state,
    export_state,
    hash_state_changes,
)
from xian.state_root import compute_exported_state_root


class StateExportTests(unittest.TestCase):
    def test_hash_state_changes_supports_bytes(self):
        digest = hash_state_changes([{"key": "con_seed.binary", "value": b"\x01\x02"}])

        self.assertEqual(len(digest), 64)

    def test_build_exported_state_sorts_entries_and_signs_origin(self):
        founder_private_key = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        contract_state = {
            "con_b.value": 2,
            "con_a.value": 1,
            "con_none.value": None,
        }
        run_state = {
            EXECUTION_POLICY_KEY: current_execution_policy(),
            "__n.alice": 7,
            "other": 9,
        }

        exported_state = build_exported_state(
            founder_private_key=founder_private_key,
            contract_state=contract_state,
            run_state=run_state,
            latest_block_hash=b"",
            latest_block_height=12,
        )

        self.assertEqual(
            exported_state["hash"],
            compute_exported_state_root(exported_state).hex(),
        )
        self.assertEqual(exported_state["number"], 12)
        self.assertEqual(
            [item["key"] for item in exported_state["genesis"]],
            [EXECUTION_POLICY_KEY, "con_a.value", "con_b.value"],
        )
        self.assertEqual(
            exported_state["nonces"],
            [{"key": "alice", "value": 7}],
        )
        self.assertTrue(exported_state["origin"]["sender"])
        self.assertTrue(exported_state["origin"]["signature"])

    def test_export_state_writes_encoded_payload(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir)
            expected_state = {
                "genesis": [
                    {"key": EXECUTION_POLICY_KEY, "value": current_execution_policy()},
                    {"key": "con_a.value", "value": 1},
                ],
                "nonces": [{"key": "alice", "value": 7}],
            }
            expected_hash = compute_exported_state_root(expected_state)
            with patch(
                "xian.state_export.fetch_filebased_state",
                return_value=(
                    {"con_a.value": 1},
                    {
                        "__n.alice": 7,
                        EXECUTION_POLICY_KEY: current_execution_policy(),
                        "__latest_block": {"hash": expected_hash.hex(), "height": 9, "nanos": 0},
                    },
                ),
            ):
                with patch(
                    "xian.state_export.get_latest_block_hash",
                    return_value=expected_hash,
                ):
                    with patch(
                        "xian.state_export.get_latest_block_height",
                        return_value=9,
                    ):
                        output_path = export_state(output_dir=output_dir)

            payload = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(output_path, output_dir / "exported_state.json")
            self.assertEqual(payload["hash"], expected_hash.hex())
            self.assertEqual(payload["number"], 9)
            self.assertEqual(
                payload["genesis"],
                [
                    {"key": EXECUTION_POLICY_KEY, "value": current_execution_policy()},
                    {"key": "con_a.value", "value": 1},
                ],
            )


if __name__ == "__main__":
    unittest.main()


def test_export_import_preserves_canonical_set_storage_bytes(tmp_path):
    from xian_runtime_types.decimal import ContractingDecimal

    from contracting.storage.driver import Driver
    from xian_runtime_types.collections import ContractingFrozenSet, ContractingSet
    from xian_runtime_types.encoding import decode

    from xian.execution_policy import initialize_execution_policy
    from xian.state_export import import_state

    source = tmp_path / "source"
    destination = tmp_path / "destination"
    initialize_execution_policy(source)
    driver = Driver(vm_profile="xian_vm_v2", storage_home=source)
    value = ContractingSet(
        [
            ("large", 9007199254740993),
            ("zero", ContractingDecimal("0")),
            ("decimal", ContractingDecimal("1.234567890123456789")),
            ContractingFrozenSet([b"\x00\xff"]),
        ]
    )
    try:
        driver.set("con_demo.stored", value)
        driver.commit()
        from contracting.storage.lmdb_environment import DEFAULT_ENVIRONMENT_POOL

        original, _ = DEFAULT_ENVIRONMENT_POOL.read_existing_value(
            source / "lmdb", b"con_demo.stored"
        )
        exported_path = export_state(output_dir=tmp_path / "export", storage_home=source)
        exported = decode(exported_path.read_text(), vm_profile="xian_vm_v2")
        import_state(exported_state=exported, storage_home=destination)
        restored = Driver(vm_profile="xian_vm_v2", storage_home=destination)
        try:
            actual, _ = DEFAULT_ENVIRONMENT_POOL.read_existing_value(
                destination / "lmdb", b"con_demo.stored"
            )
            assert actual == original
            assert isinstance(restored.get("con_demo.stored"), ContractingSet)
        finally:
            restored.close()
    finally:
        driver.close()


def _valid_import_payload():
    payload = {
        "number": 42,
        "nanos": 123,
        "genesis": [
            {"key": EXECUTION_POLICY_KEY, "value": current_execution_policy()},
            {"key": "con_import.value", "value": 7},
        ],
    }
    payload["hash"] = compute_exported_state_root(payload).hex()
    return payload


def test_import_rejects_uncommitted_runtime_metadata_before_replacing_state(tmp_path):
    import pytest
    from contracting.storage.driver import Driver
    from xian.state_export import import_state
    from xian.utils.block import get_committed_latest_block

    driver = Driver(vm_profile="xian_vm_v2", storage_home=tmp_path)
    try:
        driver.set(EXECUTION_POLICY_KEY, current_execution_policy())
        driver.set("local.value", "preserve me")
        driver.commit()
        exported = _valid_import_payload()
        exported["genesis"].append({
            "key": "__latest_block",
            "value": {"height": 999, "hash": "ff" * 32, "nanos": 0},
        })
        # Local runtime metadata does not contribute to the trusted state root.
        assert compute_exported_state_root(exported).hex() == exported["hash"]
        with pytest.raises(ValueError, match="non-consensus"):
            import_state(exported_state=exported, storage_home=tmp_path)
        driver.flush_cache()
        assert driver.get("local.value") == "preserve me"
        assert driver.get("con_import.value") is None
        assert get_committed_latest_block(driver)["height"] == 0
    finally:
        driver.close()


def test_import_commit_failure_preserves_original_durable_state(tmp_path):
    import pytest
    from contracting.storage.driver import Driver
    from xian.state_export import import_state
    from xian.utils.block import get_committed_latest_block, stage_latest_block

    driver = Driver(vm_profile="xian_vm_v2", storage_home=tmp_path)
    try:
        driver.set(EXECUTION_POLICY_KEY, current_execution_policy())
        driver.set("local.value", "preserve me")
        stage_latest_block(driver, block_hash=bytes.fromhex("ab" * 32), height=5, nanos=10)
        driver.commit()
        with patch(
            "contracting.storage.lmdb_store.LMDBStore.batch_set",
            side_effect=OSError("injected commit failure"),
        ), pytest.raises(OSError, match="injected commit failure"):
            import_state(exported_state=_valid_import_payload(), storage_home=tmp_path)
        driver.flush_cache()
        assert driver.get("local.value") == "preserve me"
        assert driver.get("con_import.value") is None
        assert get_committed_latest_block(driver)["height"] == 5
    finally:
        driver.close()


def test_import_invalid_block_metadata_preserves_original_durable_state(tmp_path):
    import pytest
    from contracting.storage.driver import Driver
    from xian.state_export import import_state

    driver = Driver(vm_profile="xian_vm_v2", storage_home=tmp_path)
    try:
        driver.set(EXECUTION_POLICY_KEY, current_execution_policy())
        driver.set("local.value", "preserve me")
        driver.commit()
        exported = _valid_import_payload()
        exported["number"] = "invalid"
        with pytest.raises(ValueError):
            import_state(exported_state=exported, storage_home=tmp_path)
        driver.flush_cache()
        assert driver.get("local.value") == "preserve me"
    finally:
        driver.close()


def test_import_replaces_old_keys_and_commits_matching_block_metadata(tmp_path):
    from contracting.storage.driver import Driver
    from xian.state_export import import_state
    from xian.utils.block import get_committed_latest_block

    driver = Driver(vm_profile="xian_vm_v2", storage_home=tmp_path)
    try:
        driver.set(EXECUTION_POLICY_KEY, current_execution_policy())
        driver.set("local.value", "remove me")
        driver.set("__n.old:", 3)
        driver.commit()
        exported = _valid_import_payload()
        import_state(exported_state=exported, storage_home=tmp_path)
        driver.flush_cache()
        assert driver.get("local.value") is None
        assert driver.get("__n.old:") is None
        assert driver.get("con_import.value") == 7
        assert get_committed_latest_block(driver) == {
            "height": 42, "hash": exported["hash"], "nanos": 123,
        }
    finally:
        driver.close()


def test_export_uses_durable_block_marker_when_json_mirror_is_stale(tmp_path):
    from contracting.storage.driver import Driver
    from xian.state_export import load_exported_state
    from xian.state_root import compute_driver_state_root
    from xian.utils.block import set_latest_block, stage_latest_block

    with Driver(vm_profile="xian_vm_v2", storage_home=tmp_path) as driver:
        driver.set(EXECUTION_POLICY_KEY, current_execution_policy())
        driver.set("con_export.value", 7)
        root = compute_driver_state_root(driver)
        stage_latest_block(driver, block_hash=root, height=42, nanos=123)
        driver.commit()
    set_latest_block(block_hash=bytes.fromhex("ff" * 32), height=1, storage_home=tmp_path)
    path = export_state(output_dir=tmp_path / "exports", storage_home=tmp_path)
    exported = load_exported_state(path)
    assert exported["hash"] == root.hex()
    assert exported["number"] == 42
    assert exported["nanos"] == 123


def test_import_defaults_missing_block_metadata_to_initial_height_and_time(tmp_path):
    from contracting.storage.driver import Driver
    from xian.state_export import import_state
    from xian.utils.block import get_committed_latest_block

    exported = _valid_import_payload()
    del exported["number"]
    del exported["nanos"]
    result = import_state(exported_state=exported, storage_home=tmp_path)
    assert result["height"] == 0
    assert result["nanos"] == 0
    with Driver(vm_profile="xian_vm_v2", storage_home=tmp_path) as driver:
        assert get_committed_latest_block(driver) == {
            "hash": exported["hash"], "height": 0, "nanos": 0,
        }


def test_prepare_rejects_duplicate_keys_with_null_values_and_nonce_collisions():
    import pytest
    from xian.state_export import prepare_state_import

    cases = [
        ([{"key": "con_import.value", "value": None}], []),
        ([{"key": "con_null.value", "value": None}] * 2, []),
        ([], [{"key": "alice:", "value": 5}, {"key": "alice:", "value": None}]),
        ([{"key": "__n.alice:", "value": 5}], [{"key": "alice:", "value": None}]),
        ([{"key": "__n.alice:", "value": None}], [{"key": "alice:", "value": 5}]),
    ]
    for genesis, nonces in cases:
        exported = _valid_import_payload()
        exported["genesis"].extend(genesis)
        exported["nonces"] = nonces
        exported["hash"] = compute_exported_state_root(exported).hex()
        with pytest.raises(ValueError, match="duplicate"):
            prepare_state_import(exported)


def test_runtime_features_are_hashed_exported_and_imported(tmp_path):
    # Runtime features change execution results, so they are consensus state:
    # in the app hash, in exports and therefore in state-sync snapshots.
    from contracting.runtime_features import runtime_feature_state_key
    from contracting.storage.driver import Driver

    from xian.state_export import export_state, import_state, load_exported_state
    from xian.state_root import compute_driver_state_root
    from xian.utils.block import stage_latest_block

    zk_key = runtime_feature_state_key("zk")
    source = tmp_path / "source"
    with Driver(vm_profile="xian_vm_v2", storage_home=source) as driver:
        driver.set(EXECUTION_POLICY_KEY, current_execution_policy())
        driver.set("con_import.value", 7)
        driver.set(zk_key, False)
        driver.commit()
        disabled_root = compute_driver_state_root(driver)
        driver.set(zk_key, True)
        driver.commit()
        enabled_root = compute_driver_state_root(driver)
        stage_latest_block(driver, block_hash=enabled_root, height=3, nanos=9)
        driver.commit()
    assert disabled_root != enabled_root

    exported = load_exported_state(export_state(output_dir=tmp_path / "out", storage_home=source))
    assert {"key": zk_key, "value": True} in exported["genesis"]

    target = tmp_path / "target"
    import_state(exported_state=exported, storage_home=target)
    with Driver(vm_profile="xian_vm_v2", storage_home=target) as driver:
        assert driver.get(zk_key) is True
        assert compute_driver_state_root(driver) == enabled_root
