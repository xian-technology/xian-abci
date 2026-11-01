import json
import tarfile
import tempfile
import unittest
from pathlib import Path

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

            target_driver = Driver(storage_home=target_home)
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

            target_driver = Driver(storage_home=target_home)
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
            live_driver = Driver(storage_home=target_home)
            self.assertIsNone(live_driver.get("demo.count"))
            target = StateSnapshotManager(storage_home=target_home, chain_id="xian-test-1")
            app = SimpleNamespace(
                client=SimpleNamespace(raw_driver=live_driver),
                state_snapshot_manager=target,
                nonce_storage=Mock(),
                state_root_cache=StateRootCache(),
                app_version=1,
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
            # Another startup must retain the imported marker rather than reset to zero.
            app.client.raw_driver = Driver(storage_home=target_home)
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
                exported_state["genesis"][0]["value"] = "tampered"
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
            with self.assertRaisesRegex(ValueError, "snapshot state root mismatch"):
                target_manager.import_snapshot_archive(tampered_path)

    @staticmethod
    def _seed_state(storage_home: Path) -> None:
        driver = Driver(storage_home=storage_home)
        driver.set(EXECUTION_POLICY_KEY, current_execution_policy())
        driver.set_contract_from_source("demo", CONTRACT_SOURCE, owner="alice")
        driver.set("demo.set", ContractingSet([(1, "a"), ContractingFrozenSet([b"x"])]))
        driver.set("demo.count", 7)
        driver.set("demo.price", ContractingDecimal("1.25"))
        driver.set(
            f"__n{contracting_constants.INDEX_SEPARATOR}alice{contracting_constants.DELIMITER}",
            5,
        )
        driver.commit()
        set_latest_block_hash(compute_driver_state_root(driver), storage_home)
        set_latest_block_height(42, storage_home)


if __name__ == "__main__":
    unittest.main()
