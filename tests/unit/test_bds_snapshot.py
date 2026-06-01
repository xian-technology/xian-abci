import json
import tarfile
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from xian.services.bds import sql
from xian.services.bds.snapshot import (
    TABLE_SPECS,
    default_snapshot_output_path,
    deserialize_snapshot_row,
    export_bds_snapshot,
    import_bds_snapshot,
    serialize_snapshot_row,
)


class _FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeAcquire:
    def __init__(self, connection):
        self._connection = connection

    async def __aenter__(self):
        return self._connection

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeConnection:
    def __init__(self, rows_by_table):
        self.rows_by_table = rows_by_table
        self.inserted = {}
        self.sequence_resets = 0

    def transaction(self):
        return _FakeTransaction()

    async def cursor(self, query):
        table = query.split("FROM ", 1)[1].split(" ", 1)[0]
        for row in self.rows_by_table.get(table, []):
            yield row

    async def executemany(self, query, records):
        table = query.split("INTO ", 1)[1].split(" ", 1)[0]
        self.inserted.setdefault(table, []).extend(records)

    async def execute(self, query):
        if "setval(" in query:
            self.sequence_resets += 1


class _FakePool:
    def __init__(self, connection):
        self._connection = connection

    def acquire(self, *, timeout=None):
        # Production DB passes an acquire timeout; fake ignores it.
        del timeout
        return _FakeAcquire(self._connection)


class _FakeDb:
    def __init__(self, connection):
        self.pool = _FakePool(connection)

    def acquire(self, *, timeout=None):
        return self.pool.acquire(timeout=timeout)


class _FakeBds:
    def __init__(self, connection, status):
        self.db = _FakeDb(connection)
        self._status = status
        self.reset_count = 0
        self.clear_spool_count = 0
        self.compact_result = {"removed_files": 0, "kept_files": 0}

    async def get_status(self):
        return self._status

    async def reset_schema(self):
        self.reset_count += 1

    def clear_spool(self):
        self.clear_spool_count += 1

    async def compact_spool(self):
        return self.compact_result


class _FakeBlockSource:
    def __init__(self):
        self.blocks = {}

    async def block(self, height: int) -> dict:
        return self.blocks[height]


class BdsSnapshotTests(unittest.IsolatedAsyncioTestCase):
    def test_default_snapshot_output_path_includes_height(self):
        output = default_snapshot_output_path(
            output_dir=Path("/tmp"),
            indexed_height=42,
            now=datetime(2026, 3, 20, tzinfo=UTC),
        )
        self.assertEqual(
            output.name,
            "xian-bds-snapshot-h42-20260320T000000Z.tar.gz",
        )

    def test_serialize_and_deserialize_roundtrip(self):
        rewards_spec = next(spec for spec in TABLE_SPECS if spec.name == "rewards")
        row = {
            "id": 7,
            "block_height": 12,
            "tx_hash": "TX-12",
            "tx_index": 0,
            "reward_index": 0,
            "type": "chi",
            "recipient_key": "currency.balances:alice",
            "source_contract": "con_token",
            "value": Decimal("12.34"),
            "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        }

        serialized = serialize_snapshot_row(rewards_spec, row)
        restored = deserialize_snapshot_row(rewards_spec, serialized)

        self.assertEqual(serialized["value"], "12.34")
        self.assertEqual(
            serialized["created_at"], "2026-01-01T00:00:00+00:00"
        )
        self.assertEqual(restored[8], Decimal("12.34"))
        self.assertEqual(restored[9], datetime(2026, 1, 1, tzinfo=UTC))

    async def test_export_and_import_snapshot_roundtrip(self):
        rows_by_table = {
            "bds_meta": [
                {
                    "key": "schema_version",
                    "value": str(sql.SCHEMA_VERSION),
                    "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
                }
            ],
            "blocks": [
                {
                    "height": 12,
                    "block_hash": "BLOCK-12",
                    "block_time": 12,
                    "block_time_iso": datetime(2026, 1, 1, tzinfo=UTC),
                    "tx_count": 1,
                    "app_hash": "APP-12",
                    "created_at": datetime(2026, 1, 1, tzinfo=UTC),
                }
            ],
            "state_changes": [
                {
                    "change_id": 5,
                    "block_height": 12,
                    "block_hash": "BLOCK-12",
                    "block_time": 12,
                    "tx_hash": "TX-12",
                    "tx_index": 0,
                    "write_index": 0,
                    "key": "currency.balances:alice",
                    "new_value": {"__fixed__": "1.0"},
                    "previous_change_id": 4,
                    "previous_tx_hash": "TX-11",
                    "origin_type": "tx",
                    "created_at": datetime(2026, 1, 1, tzinfo=UTC),
                }
            ],
            "rewards": [
                {
                    "id": 3,
                    "block_height": 12,
                    "tx_hash": "TX-12",
                    "tx_index": 0,
                    "reward_index": 0,
                    "type": "chi",
                    "recipient_key": "currency.balances:alice",
                    "source_contract": "con_token",
                    "value": Decimal("12.34"),
                    "created_at": datetime(2026, 1, 1, tzinfo=UTC),
                }
            ],
            "shielded_output_tags": [
                {
                    "id": 4,
                    "block_height": 12,
                    "tx_hash": "TX-12",
                    "tx_index": 0,
                    "contract": "con_private",
                    "function": "transfer",
                    "action": "deposit",
                    "output_index": 0,
                    "note_index": 7,
                    "commitment": "0x" + "11" * 32,
                    "new_root": "0x" + "22" * 32,
                    "payload_hash": "0x" + "33" * 32,
                    "tag_kind": "sync_hint",
                    "tag_value": "0x1234",
                    "created_at": datetime(2026, 1, 1, tzinfo=UTC),
                }
            ],
        }
        status = {
            "indexed": {
                "indexed_height": 12,
                "indexed_block_hash": "BLOCK-12",
                "indexed_app_hash": "APP-12",
            },
            "spool_pending_count": 0,
        }
        connection = _FakeConnection(rows_by_table)
        bds = _FakeBds(connection, status)

        with TemporaryDirectory() as temp_dir:
            snapshot_path = Path(temp_dir) / "snapshot.tar.gz"

            export_result = await export_bds_snapshot(
                bds=bds,
                output_path=snapshot_path,
                force=False,
            )

            self.assertEqual(export_result["indexed_height"], 12)
            self.assertTrue(snapshot_path.exists())

            with tarfile.open(snapshot_path, "r:gz") as archive:
                metadata = json.loads(
                    archive.extractfile("metadata.json").read().decode("utf-8")
                )
            self.assertEqual(metadata["tables"]["blocks"]["row_count"], 1)
            self.assertEqual(
                metadata["tables"]["state_changes"]["row_count"], 1
            )

            import_connection = _FakeConnection({})
            import_bds = _FakeBds(import_connection, status)
            trusted_block_source = _FakeBlockSource()
            trusted_block_source.blocks[12] = {
                "block_id": {"hash": "BLOCK-12"},
                "block": {"header": {"app_hash": "APP-12"}},
            }
            import_result = await import_bds_snapshot(
                bds=import_bds,
                snapshot_path=snapshot_path,
                clear_spool=True,
                trusted_block_source=trusted_block_source,
            )

            self.assertEqual(import_bds.reset_count, 1)
            self.assertEqual(import_bds.clear_spool_count, 1)
            self.assertEqual(import_connection.sequence_resets, 4)
            self.assertEqual(
                import_connection.inserted["state_changes"][0][0], 5
            )
            self.assertEqual(
                import_connection.inserted["rewards"][0][8], Decimal("12.34")
            )
            self.assertEqual(
                import_connection.inserted["shielded_output_tags"][0][12],
                "sync_hint",
            )
            self.assertEqual(
                import_connection.inserted["shielded_output_tags"][0][13],
                "0x1234",
            )
            self.assertEqual(import_result["indexed_height"], 12)

    async def test_import_snapshot_rejects_unsafe_link_member(self):
        status = {
            "indexed": {"indexed_height": None},
            "spool_pending_count": 0,
        }
        bds = _FakeBds(_FakeConnection({}), status)

        with TemporaryDirectory() as temp_dir:
            snapshot_path = Path(temp_dir) / "snapshot.tar.gz"
            metadata_path = Path(temp_dir) / "metadata.json"
            metadata_path.write_text(
                json.dumps(
                    {
                        "snapshot_format_version": 1,
                        "schema_version": sql.SCHEMA_VERSION,
                        "tables": {},
                    }
                ),
                encoding="utf-8",
            )

            with tarfile.open(snapshot_path, "w:gz") as archive:
                archive.add(metadata_path, arcname="metadata.json")
                link_info = tarfile.TarInfo("blocks.jsonl")
                link_info.type = tarfile.SYMTYPE
                link_info.linkname = "/etc/passwd"
                archive.addfile(link_info)

            with self.assertRaises(tarfile.TarError):
                await import_bds_snapshot(
                    bds=bds,
                    snapshot_path=snapshot_path,
                    clear_spool=False,
                )

    async def test_import_snapshot_rejects_indexed_hash_mismatch(self):
        status = {
            "indexed": {
                "indexed_height": 12,
                "indexed_block_hash": "BLOCK-12",
                "indexed_app_hash": "APP-12",
            },
            "spool_pending_count": 0,
        }
        bds = _FakeBds(_FakeConnection({}), status)
        trusted_block_source = _FakeBlockSource()
        trusted_block_source.blocks[12] = {
            "block_id": {"hash": "BLOCK-999"},
            "block": {"header": {"app_hash": "APP-12"}},
        }

        with TemporaryDirectory() as temp_dir:
            snapshot_path = Path(temp_dir) / "snapshot.tar.gz"
            metadata_path = Path(temp_dir) / "metadata.json"
            metadata_path.write_text(
                json.dumps(
                    {
                        "snapshot_format_version": 1,
                        "schema_version": sql.SCHEMA_VERSION,
                        "indexed": status["indexed"],
                        "tables": {},
                    }
                ),
                encoding="utf-8",
            )

            with tarfile.open(snapshot_path, "w:gz") as archive:
                archive.add(metadata_path, arcname="metadata.json")

            with self.assertRaisesRegex(ValueError, "block hash mismatch"):
                await import_bds_snapshot(
                    bds=bds,
                    snapshot_path=snapshot_path,
                    clear_spool=False,
                    trusted_block_source=trusted_block_source,
                )


if __name__ == "__main__":
    unittest.main()
