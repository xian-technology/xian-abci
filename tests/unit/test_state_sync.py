import tempfile
import unittest
from pathlib import Path

from contracting import constants as contracting_constants
from contracting.compilation.compiler import ContractingCompiler
from contracting.storage.driver import Driver
from cometbft.abci.v1beta1.types_pb2 import Snapshot
from xian_runtime_types.decimal import ContractingDecimal

from xian.services.state_sync import StateSnapshotManager
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
CANONICAL_CONTRACT_SOURCE = ContractingCompiler(
    module_name="demo"
).normalize_source(CONTRACT_SOURCE)
CANONICAL_RUNTIME_CODE = ContractingCompiler(module_name="demo").parse_to_code(
    CONTRACT_SOURCE
)


class StateSyncTests(unittest.TestCase):
    def test_snapshot_export_and_import_round_trip(self):
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as target_dir:
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
            self.assertEqual(import_result["app_hash"], "ab" * 32)
            self.assertTrue(Path(import_result["stored_snapshot_path"]).exists())

            target_driver = Driver(storage_home=target_home)
            self.assertEqual(
                target_driver.get("demo.__source__"),
                CANONICAL_CONTRACT_SOURCE,
            )
            self.assertEqual(
                target_driver.get("demo.__code__"),
                CANONICAL_RUNTIME_CODE,
            )
            self.assertIsNotNone(target_driver.get("demo.__xian_ir_v1__"))
            self.assertEqual(target_driver.get("demo.count"), 7)
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
            self.assertEqual(get_latest_block_hash(target_home), bytes.fromhex("ab" * 32))

    def test_snapshot_abci_chunk_flow_restores_target_state(self):
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as target_dir:
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
                target_driver.get("demo.price"),
                ContractingDecimal("1.25"),
            )
            self.assertEqual(get_latest_block_height(target_home), 42)
            self.assertTrue(target_manager.list_snapshot_records())

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
                    format=1,
                    chunks=1,
                    hash=bytes.fromhex("ab" * 32),
                    metadata=(
                        b'{"snapshot_format_version":1,"chain_id":"xian-test-1",'
                        b'"height":"not-an-int","app_hash":"'
                        + ("cd" * 32).encode("ascii")
                        + b'","archive_sha256":"'
                        + ("ab" * 32).encode("ascii")
                        + b'","chunk_size":64}'
                    ),
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
                    format=1,
                    chunks=1,
                    hash=bytes.fromhex("ab" * 32),
                    metadata=(
                        b'{"snapshot_format_version":1,"chain_id":"xian-test-1",'
                        b'"height":43,"app_hash":"'
                        + ("cd" * 32).encode("ascii")
                        + b'","archive_sha256":"'
                        + ("ab" * 32).encode("ascii")
                        + b'","chunk_size":65}'
                    ),
                ),
                app_hash=bytes.fromhex("cd" * 32),
                current_height=0,
            )

            self.assertEqual(response.result, response.REJECT)

    def test_apply_snapshot_chunk_rejects_oversized_chunk(self):
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as target_dir:
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

    @staticmethod
    def _seed_state(storage_home: Path) -> None:
        driver = Driver(storage_home=storage_home)
        driver.set_contract_from_source("demo", CONTRACT_SOURCE, owner="alice")
        driver.set("demo.count", 7)
        driver.set("demo.price", ContractingDecimal("1.25"))
        driver.set(
            "__n"
            f"{contracting_constants.INDEX_SEPARATOR}alice"
            f"{contracting_constants.DELIMITER}",
            5,
        )
        driver.commit()
        set_latest_block_hash(bytes.fromhex("ab" * 32), storage_home)
        set_latest_block_height(42, storage_home)


if __name__ == "__main__":
    unittest.main()
