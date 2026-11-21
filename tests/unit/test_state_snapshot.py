import io
import json
import shutil
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from contracting import constants as contracting_constants
from contracting.compilation.compiler import ContractingCompiler
from contracting.storage.driver import Driver
from xian_runtime_types.collections import ContractingFrozenSet, ContractingSet
from xian_runtime_types.decimal import ContractingDecimal
from xian_runtime_types.encoding import encode

from xian.evidence_policy import EVIDENCE_POLICY_KEY, EvidencePolicy
from xian.execution_policy import EXECUTION_POLICY_KEY, current_execution_policy
from xian.services.state_snapshot import (
    SNAPSHOT_FORMAT_VERSION,
    StateSnapshotManager,
    _safe_extract_snapshot,
)
from xian.state_export import load_exported_state
from xian.state_root import compute_driver_state_root
from xian.utils.block import (
    get_latest_block_hash,
    get_latest_block_height,
    set_latest_block_hash,
    set_latest_block_height,
    stage_latest_block,
)

CONTRACT_SOURCE = """
value = Variable()


@construct
def seed():
    value.set(1)


@export
def get():
    return value.get()
""".strip()
CANONICAL_CONTRACT_SOURCE = ContractingCompiler(module_name="demo").normalize_source(
    CONTRACT_SOURCE
)


class StateSnapshotTests(unittest.TestCase):
    def test_snapshot_rejects_unexpected_duplicate_and_link_members_before_extraction(self):
        for extra in ("padding", "duplicate", "link"):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                path = root / "snapshot.tar.gz"
                destination = root / "unpacked"
                destination.mkdir()
                with tarfile.open(path, "w:gz") as archive:
                    for name in ("metadata.json", "exported_state.json"):
                        info = tarfile.TarInfo(name)
                        info.size = 2
                        archive.addfile(info, io.BytesIO(b"{}"))
                    name = "metadata.json" if extra != "padding" else "padding.bin"
                    info = tarfile.TarInfo(name)
                    if extra == "link":
                        info.type = tarfile.SYMTYPE
                        info.linkname = "exported_state.json"
                        archive.addfile(info)
                    else:
                        data = b"0" * 1_000_000 if extra == "padding" else b"{}"
                        info.size = len(data)
                        archive.addfile(info, io.BytesIO(data))

                with tarfile.open(path) as archive:
                    with self.assertRaises(ValueError):
                        _safe_extract_snapshot(archive, destination)

                self.assertEqual(list(destination.iterdir()), [])

    def test_snapshot_can_be_imported_from_its_own_snapshot_directory(self):
        with tempfile.TemporaryDirectory() as source_dir:
            home = Path(source_dir)
            self._seed_state(home)
            manager = StateSnapshotManager(storage_home=home, chain_id="xian-test-1")
            exported = manager.export_snapshot()
            result = manager.import_snapshot_archive(Path(exported["output_path"]))
            self.assertEqual(result["height"], 42)
            records = manager.list_snapshot_records()
            self.assertEqual(len(records), 1)
            self.assertTrue(records[0].path.is_file())

    def test_export_requires_force_before_replacing_existing_snapshot(self):
        with tempfile.TemporaryDirectory() as source_dir:
            home = Path(source_dir)
            self._seed_state(home)
            manager = StateSnapshotManager(storage_home=home, chain_id="xian-test-1")
            exported = manager.export_snapshot()
            path = Path(exported["output_path"])
            original = path.read_bytes()
            with self.assertRaises(FileExistsError):
                manager.export_snapshot(output_path=path)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(len(manager.list_snapshot_records()), 1)

    def test_failed_forced_export_preserves_existing_snapshot(self):
        with tempfile.TemporaryDirectory() as source_dir:
            home = Path(source_dir)
            self._seed_state(home)
            manager = StateSnapshotManager(storage_home=home, chain_id="xian-test-1")
            exported = manager.export_snapshot()
            path = Path(exported["output_path"])
            original = path.read_bytes()
            with (
                patch("tarfile.TarFile.add", side_effect=OSError("injected archive failure")),
                self.assertRaisesRegex(OSError, "injected archive failure"),
            ):
                manager.export_snapshot(output_path=path, force=True)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(len(manager.list_snapshot_records()), 1)

    def test_snapshot_cleanup_preserves_other_heights_and_formats(self):
        with tempfile.TemporaryDirectory() as source_dir:
            home = Path(source_dir)
            self._seed_state(home)
            manager = StateSnapshotManager(storage_home=home, chain_id="xian-test-1")
            first = Path(manager.export_snapshot()["output_path"])
            with Driver(vm_profile="xian_vm_v2", storage_home=home) as driver:
                stage_latest_block(
                    driver, block_hash=compute_driver_state_root(driver), height=43, nanos=0
                )
                driver.commit()
            second = Path(manager.export_snapshot()["output_path"])
            old_format = second.with_name("old-format.tar.gz")
            shutil.copy2(second, old_format)
            manifest = json.loads(manager._manifest_path(second).read_text())
            manifest.update(archive_name=old_format.name, format=SNAPSHOT_FORMAT_VERSION - 1)
            manager._manifest_path(old_format).write_text(json.dumps(manifest))
            replacement = home / "snapshots" / "replacement.tar.gz"
            manager.export_snapshot(output_path=replacement)
            self.assertFalse(second.exists())
            self.assertFalse(manager._manifest_path(second).exists())
            self.assertEqual(
                {record.path.name for record in manager.list_snapshot_records()},
                {first.name, old_format.name, replacement.name},
            )

    def test_snapshot_publication_failure_preserves_original_durable_state(self):
        from xian.utils.block import get_committed_latest_block

        for failure in ("SnapshotLimits.stage_archive", "StateSnapshotManager._write_manifest"):
            with (
                self.subTest(failure=failure),
                tempfile.TemporaryDirectory() as source_dir,
                tempfile.TemporaryDirectory() as target_dir,
            ):
                source_home, target_home = Path(source_dir), Path(target_dir)
                self._seed_state(source_home)
                source = StateSnapshotManager(storage_home=source_home, chain_id="local")
                source.export_snapshot()
                record = source.list_snapshot_records()[0]
                target = StateSnapshotManager(storage_home=target_home, chain_id="local")
                with Driver(vm_profile="xian_vm_v2", storage_home=target_home) as driver:
                    driver.set(EXECUTION_POLICY_KEY, current_execution_policy())
                    driver.set("local.value", "preserve me")
                    stage_latest_block(
                        driver, block_hash=compute_driver_state_root(driver), height=3, nanos=1
                    )
                    driver.commit()
                    previous_snapshot = Path(target.export_snapshot()["output_path"])
                    previous_archive = previous_snapshot.read_bytes()
                    previous_manifest = target._manifest_path(previous_snapshot).read_bytes()
                    with (
                        patch(
                            "xian.services.state_snapshot." + failure,
                            side_effect=OSError("injected publication failure"),
                        ),
                        self.assertRaisesRegex(OSError, "injected publication failure"),
                    ):
                        target.import_snapshot_archive(record.path)
                    driver.flush_cache()
                    self.assertEqual(get_committed_latest_block(driver)["height"], 3)
                    self.assertEqual(driver.get("local.value"), "preserve me")
                    self.assertIsNone(driver.get("demo.count"))
                    self.assertEqual(previous_snapshot.read_bytes(), previous_archive)
                    self.assertEqual(
                        target._manifest_path(previous_snapshot).read_bytes(), previous_manifest
                    )

    def test_semantically_invalid_archive_preserves_existing_state_and_catalog(self):
        from xian.utils.block import get_committed_latest_block

        with (
            tempfile.TemporaryDirectory() as source_dir,
            tempfile.TemporaryDirectory() as target_dir,
        ):
            source_home, target_home = Path(source_dir), Path(target_dir)
            self._seed_state(source_home)
            source = StateSnapshotManager(storage_home=source_home, chain_id="local")
            original = Path(source.export_snapshot()["output_path"])
            extract_dir = source_home / "forged"
            with tarfile.open(original) as archive:
                archive.extractall(extract_dir, filter="data")
            state_path = extract_dir / "exported_state.json"
            exported = load_exported_state(state_path)
            exported["genesis"].append({"key": "__latest_block", "value": {"height": 999}})
            state_path.write_text(encode(exported, vm_profile="xian_vm_v2"))
            forged = source_home / "forged.tar.gz"
            with tarfile.open(forged, "w:gz") as archive:
                archive.add(extract_dir / "metadata.json", arcname="metadata.json")
                archive.add(state_path, arcname="exported_state.json")

            target = StateSnapshotManager(storage_home=target_home, chain_id="local")
            with Driver(vm_profile="xian_vm_v2", storage_home=target_home) as driver:
                driver.set(EXECUTION_POLICY_KEY, current_execution_policy())
                driver.set("local.value", "preserve me")
                stage_latest_block(
                    driver, block_hash=compute_driver_state_root(driver), height=3, nanos=1
                )
                driver.commit()
                previous = Path(target.export_snapshot()["output_path"])
                with self.assertRaisesRegex(ValueError, "non-consensus"):
                    target.import_snapshot_archive(forged)
                driver.flush_cache()
                self.assertEqual(get_committed_latest_block(driver)["height"], 3)
                self.assertEqual(driver.get("local.value"), "preserve me")
                self.assertIsNone(driver.get("demo.count"))
                self.assertEqual(
                    [record.path for record in target.list_snapshot_records()], [previous]
                )
                self.assertEqual(set(target.snapshot_dir.glob("*.tar.gz")), {previous})

    def test_duplicate_null_entry_is_rejected_before_catalog_publication(self):
        from xian.utils.block import get_committed_latest_block

        with (
            tempfile.TemporaryDirectory() as source_dir,
            tempfile.TemporaryDirectory() as target_dir,
        ):
            source_home, target_home = Path(source_dir), Path(target_dir)
            self._seed_state(source_home)
            source = StateSnapshotManager(storage_home=source_home, chain_id="local")
            original = Path(source.export_snapshot()["output_path"])
            extract_dir = source_home / "duplicate"
            with tarfile.open(original) as archive:
                archive.extractall(extract_dir, filter="data")
            state_path = extract_dir / "exported_state.json"
            exported = load_exported_state(state_path)
            exported["genesis"].append({"key": "demo.count", "value": None})
            state_path.write_text(encode(exported, vm_profile="xian_vm_v2"))
            forged = source_home / "duplicate.tar.gz"
            with tarfile.open(forged, "w:gz") as archive:
                archive.add(extract_dir / "metadata.json", arcname="metadata.json")
                archive.add(state_path, arcname="exported_state.json")

            target = StateSnapshotManager(storage_home=target_home, chain_id="local")
            with Driver(vm_profile="xian_vm_v2", storage_home=target_home) as driver:
                driver.set(EXECUTION_POLICY_KEY, current_execution_policy())
                driver.set("local.value", "preserve me")
                driver.commit()
                with self.assertRaises(ValueError):
                    target.import_snapshot_archive(forged)
                self.assertEqual(target.list_snapshot_records(), [])
                self.assertEqual(list(target.snapshot_dir.glob("*.tar.gz")), [])
                driver.flush_cache()
                self.assertEqual(driver.get("local.value"), "preserve me")
                self.assertIsNone(driver.get("demo.count"))
                self.assertEqual(get_committed_latest_block(driver)["height"], 0)

    def test_offline_import_cleanup_failure_keeps_committed_state(self):
        with (
            tempfile.TemporaryDirectory() as source_dir,
            tempfile.TemporaryDirectory() as target_dir,
        ):
            source_home, target_home = Path(source_dir), Path(target_dir)
            self._seed_state(source_home)
            source = StateSnapshotManager(storage_home=source_home, chain_id="local")
            snapshot = source.export_snapshot()
            target = StateSnapshotManager(storage_home=target_home, chain_id="local")
            with Driver(vm_profile="xian_vm_v2", storage_home=target_home) as driver:
                driver.set(EXECUTION_POLICY_KEY, current_execution_policy())
                driver.set("local.value", "replace me")
                driver.commit()
            with patch.object(
                target, "_cleanup_existing_height", side_effect=OSError("injected cleanup failure")
            ):
                result = target.import_snapshot_archive(Path(snapshot["output_path"]))
            self.assertEqual(result["height"], 42)
            with Driver(vm_profile="xian_vm_v2", storage_home=target_home) as restarted:
                self.assertIsNone(restarted.get("local.value"))
                self.assertEqual(restarted.get("demo.count"), 7)
                self.assertEqual(get_latest_block_height(target_home), 42)

    def test_snapshot_export_and_import_round_trip(self):
        with (
            tempfile.TemporaryDirectory() as source_dir,
            tempfile.TemporaryDirectory() as target_dir,
        ):
            source_home = Path(source_dir) / "xian"
            target_home = Path(target_dir) / "xian"
            self._seed_state(source_home)

            source_manager = StateSnapshotManager(
                storage_home=source_home,
                chain_id="xian-test-1",
                chunk_size=64,
            )
            snapshot_path = Path(source_dir) / "snapshot.tar.gz"
            result = source_manager.export_snapshot(output_path=snapshot_path)

            self.assertEqual(result["height"], 42)
            self.assertGreaterEqual(result["chunks"], 1)

            target_manager = StateSnapshotManager(
                storage_home=target_home,
                chain_id="xian-test-1",
                chunk_size=64,
            )
            import_result = target_manager.import_snapshot_archive(snapshot_path)

            self.assertEqual(import_result["height"], 42)
            self.assertEqual(import_result["app_hash"], result["app_hash"])
            self.assertTrue(Path(import_result["stored_snapshot_path"]).exists())

            target_driver = Driver(vm_profile="xian_vm_v2", storage_home=target_home)
            self.addCleanup(target_driver.close)
            self.assertEqual(
                target_driver.get("demo.__source__"),
                CANONICAL_CONTRACT_SOURCE,
            )
            self.assertIsNotNone(target_driver.get("demo.__xian_ir_v1__"))
            self.assertEqual(target_driver.get("demo.count"), 7)
            self.assertEqual(
                target_driver.get("demo.set"),
                ContractingSet([(1, "a"), ContractingFrozenSet([b"x"])]),
            )
            self.assertEqual(
                target_driver.get("demo.price"),
                ContractingDecimal("1.25"),
            )
            self.assertEqual(
                target_driver.get(
                    "__n"
                    f"{contracting_constants.INDEX_SEPARATOR}alice"
                    f"{contracting_constants.DELIMITER}"
                ),
                5,
            )
            self.assertEqual(get_latest_block_height(target_home), 42)
            self.assertEqual(
                get_latest_block_hash(target_home),
                bytes.fromhex(result["app_hash"]),
            )

    def test_offline_import_refuses_a_mismatched_expected_checkpoint(self):
        with (
            tempfile.TemporaryDirectory() as source_dir,
            tempfile.TemporaryDirectory() as target_dir,
        ):
            source_home, target_home = Path(source_dir), Path(target_dir)
            self._seed_state(source_home)
            source = StateSnapshotManager(storage_home=source_home, chain_id="xian-test-1")
            source.export_snapshot()
            record = source.list_snapshot_records()[0]
            target = StateSnapshotManager(storage_home=target_home, chain_id="xian-test-1")
            for checkpoint in ({"expected_height": 43}, {"expected_app_hash_hex": "cd" * 32}):
                with (
                    self.subTest(checkpoint=checkpoint),
                    self.assertRaisesRegex(ValueError, "does not match expected checkpoint"),
                ):
                    target.import_snapshot_archive(record.path, **checkpoint)
            self.assertEqual(target.list_snapshot_records(), [])
            with Driver(vm_profile="xian_vm_v2", storage_home=target_home) as driver:
                self.assertIsNone(driver.get("demo.count"))

    def test_offline_import_reports_committed_height_after_restart(self):
        import asyncio
        from types import SimpleNamespace

        from xian.methods.info import info

        with (
            tempfile.TemporaryDirectory() as source_dir,
            tempfile.TemporaryDirectory() as target_dir,
        ):
            source_home, target_home = Path(source_dir), Path(target_dir)
            self._seed_state(source_home)
            source = StateSnapshotManager(
                storage_home=source_home, chain_id="xian-test-1", chunk_size=128
            )
            source.export_snapshot()
            record = source.list_snapshot_records()[0]
            target = StateSnapshotManager(storage_home=target_home, chain_id="xian-test-1")
            target.import_snapshot_archive(
                record.path,
                expected_height=record.height,
                expected_app_hash_hex=record.app_hash_hex,
            )
            with Driver(vm_profile="xian_vm_v2", storage_home=target_home) as restarted:
                app = SimpleNamespace(client=SimpleNamespace(raw_driver=restarted), app_version=1)
                response = asyncio.run(info(app, None))
                self.assertEqual(response.last_block_height, 42)
                self.assertEqual(response.last_block_app_hash, bytes.fromhex(record.app_hash_hex))
                self.assertEqual(restarted.get("demo.count"), 7)
                self.assertEqual(restarted.get("__n.alice:"), 5)

    def test_snapshot_import_rejects_tampered_exported_state(self):
        with (
            tempfile.TemporaryDirectory() as source_dir,
            tempfile.TemporaryDirectory() as target_dir,
        ):
            source_home = Path(source_dir) / "xian"
            target_home = Path(target_dir) / "xian"
            self._seed_state(source_home)

            source_manager = StateSnapshotManager(
                storage_home=source_home,
                chain_id="xian-test-1",
                chunk_size=64,
            )
            snapshot_path = Path(source_dir) / "snapshot.tar.gz"
            source_manager.export_snapshot(output_path=snapshot_path)

            tampered_path = Path(source_dir) / "tampered.tar.gz"
            with tempfile.TemporaryDirectory() as extract_dir:
                extract_root = Path(extract_dir)
                with tarfile.open(snapshot_path, "r:gz") as archive:
                    archive.extractall(extract_root, filter="data")
                exported_state_path = extract_root / "exported_state.json"
                exported_state = load_exported_state(exported_state_path)
                entry = next(
                    item for item in exported_state["genesis"] if item["key"] == "demo.count"
                )
                entry["value"] = "tampered"
                exported_state_path.write_text(
                    encode(exported_state),
                    encoding="utf-8",
                )
                with tarfile.open(tampered_path, "w:gz") as archive:
                    archive.add(
                        extract_root / "metadata.json",
                        arcname="metadata.json",
                    )
                    archive.add(
                        exported_state_path,
                        arcname="exported_state.json",
                    )

            target_manager = StateSnapshotManager(
                storage_home=target_home,
                chain_id="xian-test-1",
                chunk_size=64,
            )
            with self.assertRaisesRegex(ValueError, "state root mismatch"):
                target_manager.import_snapshot_archive(tampered_path)

    @staticmethod
    def _seed_state(storage_home: Path) -> None:
        driver = Driver(vm_profile="xian_vm_v2", storage_home=storage_home)
        driver.set(EXECUTION_POLICY_KEY, current_execution_policy())
        driver.set(EVIDENCE_POLICY_KEY, EvidencePolicy.seed(7, nanos=0).to_state())
        driver.set_contract_from_source("demo", CONTRACT_SOURCE, owner="alice")
        driver.set("demo.set", ContractingSet([(1, "a"), ContractingFrozenSet([b"x"])]))
        driver.set("demo.count", 7)
        driver.set("demo.price", ContractingDecimal("1.25"))
        driver.set(
            f"__n{contracting_constants.INDEX_SEPARATOR}alice{contracting_constants.DELIMITER}",
            5,
        )
        stage_latest_block(driver, block_hash=compute_driver_state_root(driver), height=42, nanos=0)
        driver.commit()
        set_latest_block_hash(compute_driver_state_root(driver), storage_home)
        set_latest_block_height(42, storage_home)
        driver.close()


if __name__ == "__main__":
    unittest.main()
