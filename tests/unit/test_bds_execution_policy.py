"""BDS boundaries reject old policy artifacts before state or files change."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock

import pytest
from xian_runtime_types.collections import ContractingSet

from xian.execution_policy import current_execution_policy
from xian.services.bds import sql
from xian.services.bds.bds import BDS
from xian.services.bds.config import BdsConfig
from xian.services.bds.payloads import BdsBlockPayload, BdsTransactionPayload


def payload(height=1):
    return BdsBlockPayload(
        block_meta={"height": height, "hash": "BLOCK", "nanos": height},
        block_time=datetime(2026, 1, 1, tzinfo=UTC),
        app_hash="APP",
    )


@pytest.mark.parametrize("policy", [None, {}, dict(current_execution_policy(), set_codec="legacy")])
def test_existing_database_policy_is_checked_before_schema_writes(policy):
    bds = BDS(BdsConfig())

    async def fetch(query, *args):
        if "to_regclass" in query:
            return "bds_meta"
        if "execution_policy" in query:
            return json.dumps(policy) if policy is not None else None
        if "schema_version" in query:
            return str(sql.SCHEMA_VERSION)
        return True

    bds.db.fetchval = AsyncMock(side_effect=fetch)
    bds.db.execute = AsyncMock()
    with pytest.raises(ValueError, match="execution policy"):
        asyncio.run(bds.ensure_schema())
    bds.db.execute.assert_not_awaited()


@pytest.mark.parametrize(
    "version,policy",
    [(1, None), (2, None), (2, {}), (2, dict(current_execution_policy(), gas_schedule="old"))],
)
def test_spool_rejects_missing_or_wrong_policy(version, policy):
    data = payload().to_spool_dict()
    data["version"] = version
    if policy is None:
        data.pop("execution_policy", None)
    else:
        data["execution_policy"] = policy
    with pytest.raises(ValueError):
        BdsBlockPayload.from_spool_dict(data)


def test_compaction_preserves_all_files_if_one_has_incompatible_policy():
    with TemporaryDirectory() as directory:
        bds = BDS(BdsConfig(spool_dir=directory))
        bds.db.fetchval = AsyncMock(return_value=10)
        first = bds._write_spool_file(payload(1))
        bad = payload(2).to_spool_dict()
        bad["version"] = 1
        bad.pop("execution_policy", None)
        second = Path(directory) / "00000000000000000002-OLD.json"
        second.write_text(json.dumps(bad))
        with pytest.raises(ValueError):
            asyncio.run(bds.compact_spool())
        assert first.exists() and second.exists()


def test_typed_set_spool_roundtrip_can_be_republished_without_losing_types():
    original = payload()
    tx = BdsTransactionPayload(
        0,
        {"payload": {}},
        {},
        {"state": [{"key": "con.values", "value": ContractingSet([1, "1"])}], "result": None},
    )
    original = BdsBlockPayload(original.block_meta, original.block_time, original.app_hash, [tx])
    encoded = original.to_spool_dict()
    recovered = BdsBlockPayload.from_spool_dict(json.loads(json.dumps(encoded)))
    assert isinstance(recovered.transactions[0].tx_result["state"][0]["value"], ContractingSet)
    assert recovered.to_spool_dict() == encoded


def _snapshot_archive(directory, *, policy=None, row_policy=None, omit=None):
    import tarfile

    from xian.services.bds.snapshot import SNAPSHOT_FORMAT_VERSION, TABLE_SPECS

    root = Path(directory)
    metadata = {
        "snapshot_format_version": SNAPSHOT_FORMAT_VERSION,
        "schema_version": sql.SCHEMA_VERSION,
        "indexed": {"indexed_height": None},
        "tables": {},
    }
    if policy is not None:
        metadata["execution_policy"] = policy
    (root / "metadata.json").write_text(json.dumps(metadata))
    for spec in TABLE_SPECS:
        rows = []
        if spec.name == "bds_meta":
            rows = [
                {
                    "key": "schema_version",
                    "value": str(sql.SCHEMA_VERSION),
                    "updated_at": "2026-01-01T00:00:00+00:00",
                }
            ]
            if row_policy is not None:
                rows.append(
                    {
                        "key": "execution_policy",
                        "value": json.dumps(row_policy),
                        "updated_at": "2026-01-01T00:00:00+00:00",
                    }
                )
        metadata["tables"][spec.name] = {"row_count": len(rows)}
        if spec.name != omit:
            (root / f"{spec.name}.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows)
            )
    (root / "metadata.json").write_text(json.dumps(metadata))
    archive = root / "snapshot.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for path in root.glob("*.json*"):
            tar.add(path, arcname=path.name)
    return archive


@pytest.mark.parametrize(
    "policy,row_policy,omit",
    [
        (None, None, None),
        (current_execution_policy(), {}, None),
        (current_execution_policy(), current_execution_policy(), "state"),
    ],
)
def test_snapshot_preflight_rejects_before_reset(policy, row_policy, omit):
    from xian.services.bds.snapshot import import_bds_snapshot

    bds = BDS(BdsConfig())
    bds.reset_schema = AsyncMock()
    with TemporaryDirectory() as directory:
        archive = _snapshot_archive(directory, policy=policy, row_policy=row_policy, omit=omit)
        with pytest.raises((ValueError, FileNotFoundError)):
            asyncio.run(import_bds_snapshot(bds=bds, snapshot_path=archive, clear_spool=True))
    bds.reset_schema.assert_not_awaited()


def test_reindex_rejects_legacy_source_before_reading_blocks():
    from types import SimpleNamespace

    from xian.services.bds.reindex import BdsReindexer

    source = SimpleNamespace(
        genesis=AsyncMock(return_value={"abci_genesis": {"genesis": []}}), block=AsyncMock()
    )
    indexer = BdsReindexer(bds=None, block_source=source, state_patch_manager=None)
    with pytest.raises(ValueError, match="execution policy"):
        asyncio.run(indexer.build_payload(1))
    source.block.assert_not_awaited()


def test_reindex_decodes_typed_sets_in_results_only():
    import base64

    from xian_runtime_types.encoding import encode

    from xian.services.bds.reindex import BdsReindexer

    wire = encode(
        {
            "state": [{"key": "con.s", "value": ContractingSet([True, 2])}],
            "result": ContractingSet(["a"]),
        }
    )
    result = BdsReindexer._decode_tx_result_data(base64.b64encode(wire.encode()).decode())
    assert isinstance(result["result"], ContractingSet)
    assert isinstance(result["state"][0]["value"], ContractingSet)


def test_direct_persistence_rejects_database_policy_before_block_query():
    bds = BDS(BdsConfig())
    bds.db.fetchval = AsyncMock(return_value=json.dumps({}))
    bds.db.fetchrow = AsyncMock()
    with pytest.raises(ValueError, match="execution policy"):
        asyncio.run(bds.persist_block(payload()))
    bds.db.fetchrow.assert_not_awaited()


def test_typed_state_and_result_direct_and_spool_persistence_are_identical():
    from test_bds_persistence import _FakeConnection
    from xian_runtime_types.encoding import decode

    value = ContractingSet([True, 2, "2", None])
    tx = BdsTransactionPayload(
        0,
        {"payload": {"kwargs": {"literal": {"__fixed__": "3"}}}},
        {
            "sender": "alice",
            "nonce": 1,
            "contract": "con_test",
            "function": "run",
            "kwargs": {"literal": {"__fixed__": "3"}},
        },
        {
            "hash": "TX",
            "status": 0,
            "chi_used": 1,
            "state": [{"key": "con.values", "value": value}],
            "result": value,
            "events": [],
            "rewards": {},
        },
    )
    original = payload()
    original = BdsBlockPayload(original.block_meta, original.block_time, original.app_hash, [tx])
    recovered = BdsBlockPayload.from_spool_dict(original.to_spool_dict())
    records = []
    for transaction in [tx, recovered.transactions[0]]:
        bds = BDS(BdsConfig())
        connection = _FakeConnection()
        asyncio.run(
            bds._persist_transaction(
                connection,
                block_meta=original.block_meta,
                block_time=original.block_time,
                current_index={},
                tx=transaction,
            )
        )
        records.append((connection.execute_calls, connection.fetchval_calls))
        inserted = next(
            args for query, args in connection.execute_calls if query == sql.insert_transaction()
        )
        assert isinstance(decode(inserted[12]), ContractingSet)
        assert json.loads(inserted[13])["kwargs"]["literal"] == {"__fixed__": "3"}
        assert isinstance(decode(connection.fetchval_calls[0][1][7]), ContractingSet)
    assert records[0] == records[1]


def test_fresh_database_stamps_schema_and_policy_in_transaction():
    from test_bds_persistence import _FakeConnection, _FakePool

    bds = BDS(BdsConfig())
    connection = _FakeConnection()
    bds.db.pool = _FakePool(connection)
    bds.db.fetchval = AsyncMock(side_effect=[None, False])
    asyncio.run(bds.ensure_schema())
    stamped = next(
        args for query, args in connection.execute_calls if query == sql.upsert_execution_policy()
    )
    assert json.loads(stamped[0]) == current_execution_policy()
    assert any(query == sql.upsert_schema_version() for query, args in connection.execute_calls)


@pytest.mark.parametrize("policy", [{}, current_execution_policy()])
def test_snapshot_export_policy_and_typed_jsonb_roundtrip(policy):
    from test_bds_snapshot import _FakeBds, _FakeConnection
    from xian_runtime_types.encoding import decode, encode

    from xian.services.bds.snapshot import export_bds_snapshot, import_bds_snapshot

    now = datetime(2026, 1, 1, tzinfo=UTC)
    wire = encode(ContractingSet([1, "1", True, None]))
    rows = {
        "bds_meta": [
            {"key": "schema_version", "value": str(sql.SCHEMA_VERSION), "updated_at": now},
            {"key": "execution_policy", "value": json.dumps(policy), "updated_at": now},
        ],
        "state": [
            {
                "key": "con.s",
                "value": wire,
                "last_change_id": 1,
                "last_tx_hash": "TX",
                "last_block_height": 1,
                "updated_at": now,
            }
        ],
    }
    status = {"indexed": {"indexed_height": None}, "spool_pending_count": 0}
    with TemporaryDirectory() as directory:
        path = Path(directory) / "snapshot.tar.gz"
        bds = _FakeBds(_FakeConnection(rows), status)
        if not policy:
            with pytest.raises(ValueError, match="execution policy"):
                asyncio.run(export_bds_snapshot(bds=bds, output_path=path))
            assert not path.exists()
            return
        asyncio.run(export_bds_snapshot(bds=bds, output_path=path))
        connection = _FakeConnection({})
        destination = _FakeBds(connection, status)
        asyncio.run(import_bds_snapshot(bds=destination, snapshot_path=path))
        restored = connection.inserted["state"][0][1]
        assert restored == wire
        assert isinstance(decode(restored), ContractingSet)


def test_reindex_reset_rejects_legacy_rpc_before_storage_initialization():
    from types import SimpleNamespace
    from unittest.mock import patch

    from xian.services.bds.reindex import run_bds_reindex

    source = AsyncMock()
    source.genesis.return_value = {"abci_genesis": {"genesis": []}}
    bds = AsyncMock()
    genesis = {
        "abci_genesis": {
            "genesis": [{"key": "__execution_policy", "value": current_execution_policy()}]
        }
    }
    with (
        patch("xian.services.bds.reindex.load_genesis_data", return_value=genesis),
        patch("xian.services.bds.reindex.resolve_bds_config"),
        patch("xian.services.bds.reindex.BDS", return_value=bds),
        patch("xian.services.bds.reindex.resolve_rpc_url", return_value="http://127.0.0.1:26657"),
        patch("xian.services.bds.reindex.CometBftRpcClient", return_value=source),
        patch("xian.services.bds.reindex.Driver"),
        patch("xian.services.bds.reindex.StatePatchManager"),
        patch("xian.services.bds.reindex.resolve_state_patch_dir", return_value="/tmp/patches"),
    ):
        with pytest.raises(ValueError, match="execution policy"):
            asyncio.run(
                run_bds_reindex(constants=SimpleNamespace(STORAGE_HOME="unused"), reset=True)
            )
    bds.initialize_storage.assert_not_awaited()
    source.close.assert_awaited_once()


def test_reindex_malformed_json_is_rejected_instead_of_skipping_transaction():
    import base64

    from xian.services.bds.reindex import BdsReindexer

    with pytest.raises(ValueError):
        BdsReindexer._decode_tx_result_data(base64.b64encode(b"{broken").decode())
