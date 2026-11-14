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

from cometbft.abci.v1beta1.types_pb2 import Snapshot
from xian.execution_policy import EXECUTION_POLICY_KEY, current_execution_policy
from xian.services.state_sync import SNAPSHOT_FORMAT_VERSION, StateSnapshotManager
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


def snapshot_metadata(**changes):
    return json.dumps(
        {
            "snapshot_format_version": SNAPSHOT_FORMAT_VERSION,
            "execution_policy": current_execution_policy(),
            "chain_id": "xian-test-1",
            "height": 43,
            "app_hash": "cd" * 32,
            "archive_sha256": "ab" * 32,
            "chunk_size": 64,
            **changes,
        }
    ).encode()


class StateSyncTests(unittest.TestCase):
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

        for failure in ("shutil.copy2", "StateSnapshotManager._write_manifest"):
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
                    offered = target.offer_snapshot_response(
                        record.to_proto(), app_hash=bytes.fromhex(record.app_hash_hex), current_height=3
                    )
                    self.assertEqual(offered.result, offered.ACCEPT)
                    with patch(
                        "xian.services.state_sync." + failure,
                        side_effect=OSError("injected publication failure"),
                    ):
                        response = target.apply_snapshot_chunk_response(
                            index=0, chunk=record.path.read_bytes(), sender="peer"
                        )
                    self.assertEqual(response.result, response.REJECT_SNAPSHOT)
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

    def test_post_import_cleanup_failure_still_accepts_and_reconciles_live_state(self):
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import Mock

        from xian.methods.info import info
        from xian.state_root import StateRootCache
        from xian.xian_abci import Xian

        for failure in ("retention", "session_cleanup"):
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
                    driver.set("local.value", "replace me")
                    driver.commit()
                    self.assertIsNone(driver.get("demo.count"))
                    app = SimpleNamespace(
                        client=SimpleNamespace(raw_driver=driver),
                        state_snapshot_manager=target,
                        nonce_storage=Mock(),
                        state_root_cache=StateRootCache.from_driver(driver),
                        app_version=1,
                        refresh_tx_fee_policy=Mock(),
                        refresh_runtime_features=Mock(),
                    )
                    offered = target.offer_snapshot_response(
                        record.to_proto(), app_hash=bytes.fromhex(record.app_hash_hex), current_height=0
                    )
                    self.assertEqual(offered.result, offered.ACCEPT)
                    session = target._incoming_session
                    failed_object, failed_method = (
                        (target, "_cleanup_existing_height")
                        if failure == "retention" else (session.temp_dir, "cleanup")
                    )
                    with patch.object(
                        failed_object, failed_method, side_effect=OSError("injected cleanup failure")
                    ):
                        response = asyncio.run(Xian.apply_snapshot_chunk(app, SimpleNamespace(
                            index=0, chunk=record.path.read_bytes(), sender="peer"
                        )))
                    self.assertEqual(response.result, response.ACCEPT)
                    self.assertIsNone(driver.get("local.value"))
                    self.assertEqual(driver.get("demo.count"), 7)
                    self.assertEqual(app.state_root_cache.root_hash.hex(), record.app_hash_hex)
                    self.assertEqual(asyncio.run(info(app, None)).last_block_height, 42)
                    session.close()

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

    def test_snapshot_abci_chunk_flow_restores_target_state(self):
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
            source_manager.export_snapshot()
            record = source_manager.list_snapshot_records()[0]

            target_manager = StateSnapshotManager(
                storage_home=target_home,
                chain_id="xian-test-1",
                chunk_size=64,
            )
            offer = target_manager.offer_snapshot_response(
                record.to_proto(),
                app_hash=bytes.fromhex(record.app_hash_hex),
                current_height=0,
            )
            self.assertEqual(offer.result, offer.ACCEPT)

            for index in range(record.chunks):
                chunk_response = source_manager.load_snapshot_chunk_response(
                    height=record.height,
                    format_version=record.format,
                    chunk_index=index,
                )
                apply_response = target_manager.apply_snapshot_chunk_response(
                    index=index,
                    chunk=chunk_response.chunk,
                    sender="peer-1",
                )
                self.assertEqual(apply_response.result, apply_response.ACCEPT)

            target_driver = Driver(vm_profile="xian_vm_v2", storage_home=target_home)
            self.addCleanup(target_driver.close)
            self.assertEqual(target_driver.get("demo.count"), 7)
            self.assertEqual(
                target_driver.get("demo.set"),
                ContractingSet([(1, "a"), ContractingFrozenSet([b"x"])]),
            )
            self.assertEqual(
                target_driver.get("demo.price"),
                ContractingDecimal("1.25"),
            )
            self.assertEqual(get_latest_block_height(target_home), 42)
            self.assertTrue(target_manager.list_snapshot_records())

    def test_state_sync_rejects_archive_that_disagrees_with_trusted_offer_before_import(self):
        for mismatch in ("app_hash", "height"):
            with (
                self.subTest(mismatch=mismatch),
                tempfile.TemporaryDirectory() as source_dir,
                tempfile.TemporaryDirectory() as target_dir,
            ):
                source_home, target_home = Path(source_dir), Path(target_dir)
                self._seed_state(source_home)
                source = StateSnapshotManager(
                    storage_home=source_home, chain_id="xian-test-1", chunk_size=1024 * 1024
                )
                source.export_snapshot()
                record = source.list_snapshot_records()[0]
                offered = record.to_proto()
                trusted_hash = record.app_hash_hex if mismatch == "height" else "cd" * 32
                trusted_height = record.height + (mismatch == "height")
                offered.height = trusted_height
                metadata = json.loads(offered.metadata)
                metadata.update(app_hash=trusted_hash, height=trusted_height)
                offered.metadata = json.dumps(metadata).encode()

                driver = Driver(vm_profile="xian_vm_v2", storage_home=target_home)
                driver.set(EXECUTION_POLICY_KEY, current_execution_policy())
                driver.set("local.value", "preserve me")
                driver.commit()
                target = StateSnapshotManager(
                    storage_home=target_home, chain_id="xian-test-1", chunk_size=1024 * 1024
                )
                offer = target.offer_snapshot_response(
                    offered, app_hash=bytes.fromhex(trusted_hash), current_height=0
                )
                self.assertEqual(offer.result, offer.ACCEPT)
                for index in range(record.chunks):
                    chunk = source.load_snapshot_chunk_response(
                        height=record.height, format_version=record.format, chunk_index=index
                    )
                    response = target.apply_snapshot_chunk_response(
                        index=index, chunk=chunk.chunk, sender="peer"
                    )
                self.assertEqual(response.result, response.REJECT_SNAPSHOT)
                driver.flush_cache()
                self.assertEqual(driver.get("local.value"), "preserve me")
                self.assertIsNone(driver.get("demo.count"))
                self.assertEqual(get_latest_block_height(target_home), 0)
                driver.close()

    def test_import_reports_committed_height_to_an_already_running_application(self):
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import Mock

        from xian.methods.info import info
        from xian.state_root import StateRootCache
        from xian.xian_abci import Xian

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
            live_driver = Driver(vm_profile="xian_vm_v2", storage_home=target_home)
            self.addCleanup(live_driver.close)
            self.assertIsNone(live_driver.get("demo.count"))
            target = StateSnapshotManager(storage_home=target_home, chain_id="xian-test-1")
            app = SimpleNamespace(
                client=SimpleNamespace(raw_driver=live_driver),
                state_snapshot_manager=target,
                nonce_storage=Mock(),
                state_root_cache=StateRootCache(),
                app_version=1,
                refresh_tx_fee_policy=Mock(),
                refresh_runtime_features=Mock(),
            )
            target.offer_snapshot_response(
                record.to_proto(), app_hash=bytes.fromhex(record.app_hash_hex), current_height=0
            )
            for index in range(record.chunks):
                chunk = source.load_snapshot_chunk_response(
                    height=record.height, format_version=record.format, chunk_index=index
                )
                response = asyncio.run(
                    Xian.apply_snapshot_chunk(
                        app, SimpleNamespace(index=index, chunk=chunk.chunk, sender="source")
                    )
                )
                self.assertEqual(response.result, response.ACCEPT)
            response = asyncio.run(info(app, None))
            self.assertEqual(response.last_block_height, 42)
            self.assertEqual(response.last_block_app_hash, bytes.fromhex(record.app_hash_hex))
            self.assertEqual(live_driver.get("demo.count"), 7)
            self.assertEqual(live_driver.get("__n.alice:"), 5)
            # Imported state may carry a different chain fee policy and runtime features.
            app.refresh_tx_fee_policy.assert_called()
            app.refresh_runtime_features.assert_called()
            # Another startup must retain the imported marker rather than reset to zero.
            app.client.raw_driver = Driver(vm_profile="xian_vm_v2", storage_home=target_home)
            self.addCleanup(app.client.raw_driver.close)
            restarted = asyncio.run(info(app, None))
            self.assertEqual(restarted.last_block_height, 42)

    def test_offer_snapshot_rejects_malformed_metadata_without_raising(self):
        with tempfile.TemporaryDirectory() as target_dir:
            target_home = Path(target_dir) / "xian"
            manager = StateSnapshotManager(
                storage_home=target_home,
                chain_id="xian-test-1",
                chunk_size=64,
            )

            response = manager.offer_snapshot_response(
                Snapshot(
                    height=43,
                    format=SNAPSHOT_FORMAT_VERSION,
                    chunks=1,
                    hash=bytes.fromhex("ab" * 32),
                    metadata=snapshot_metadata(height="not-an-int"),
                ),
                app_hash=bytes.fromhex("cd" * 32),
                current_height=0,
            )

            self.assertEqual(response.result, response.REJECT)

    def test_offer_snapshot_rejects_oversized_chunk_metadata(self):
        with tempfile.TemporaryDirectory() as target_dir:
            target_home = Path(target_dir) / "xian"
            manager = StateSnapshotManager(
                storage_home=target_home,
                chain_id="xian-test-1",
                chunk_size=64,
            )

            response = manager.offer_snapshot_response(
                Snapshot(
                    height=43,
                    format=SNAPSHOT_FORMAT_VERSION,
                    chunks=1,
                    hash=bytes.fromhex("ab" * 32),
                    metadata=snapshot_metadata(chunk_size=65),
                ),
                app_hash=bytes.fromhex("cd" * 32),
                current_height=0,
            )

            self.assertEqual(response.result, response.REJECT)

    def test_apply_snapshot_chunk_rejects_oversized_chunk(self):
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
            source_manager.export_snapshot()
            record = source_manager.list_snapshot_records()[0]

            target_manager = StateSnapshotManager(
                storage_home=target_home,
                chain_id="xian-test-1",
                chunk_size=64,
            )
            offer = target_manager.offer_snapshot_response(
                record.to_proto(),
                app_hash=bytes.fromhex(record.app_hash_hex),
                current_height=0,
            )
            self.assertEqual(offer.result, offer.ACCEPT)

            response = target_manager.apply_snapshot_chunk_response(
                index=0,
                chunk=b"x" * 65,
                sender="peer-1",
            )

            self.assertEqual(response.result, response.REJECT_SNAPSHOT)

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
                entry = next(item for item in exported_state["genesis"] if item["key"] == "demo.count")
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
        driver.set_contract_from_source("demo", CONTRACT_SOURCE, owner="alice")
        driver.set("demo.set", ContractingSet([(1, "a"), ContractingFrozenSet([b"x"])]))
        driver.set("demo.count", 7)
        driver.set("demo.price", ContractingDecimal("1.25"))
        driver.set(
            f"__n{contracting_constants.INDEX_SEPARATOR}alice{contracting_constants.DELIMITER}",
            5,
        )
        stage_latest_block(
            driver, block_hash=compute_driver_state_root(driver), height=42, nanos=0
        )
        driver.commit()
        set_latest_block_hash(compute_driver_state_root(driver), storage_home)
        set_latest_block_height(42, storage_home)
        driver.close()


if __name__ == "__main__":
    unittest.main()
