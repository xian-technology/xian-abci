"""Unmodified upstream Comet uses replay or verified complete node stores."""

import asyncio
from io import BytesIO
from unittest.mock import patch

import pytest
from test_evidence_retention import _paid_policy_block
from test_evidence_retention import app as app

from abci.server import ProtocolHandler
from abci.utils import read_messages
from tendermint.abci.types_pb2 import (
    Request,
    RequestApplySnapshotChunk,
    RequestEcho,
    RequestListSnapshots,
    RequestLoadSnapshotChunk,
    RequestOfferSnapshot,
    Response,
    ResponseApplySnapshotChunk,
    ResponseOfferSnapshot,
    Snapshot,
)
from xian.services.state_snapshot import StateSnapshotManager
from xian.xian_abci import Xian


def call(app, method, request):
    raw = asyncio.run(ProtocolHandler(app).process(method, Request(**{method: request})))
    return getattr(next(read_messages(BytesIO(raw), Response)), method)


def test_node_startup_does_not_create_offline_snapshot_directories(app):
    assert not (app.constants.STORAGE_HOME / "snapshots").exists()


def test_protocol_never_advertises_or_applies_application_snapshots(app):
    _paid_policy_block(app, [])
    asyncio.run(app.commit())
    manager = StateSnapshotManager(storage_home=app.constants.STORAGE_HOME, chain_id=app.chain_id)
    manager.export_snapshot()
    [record] = manager.list_snapshot_records()
    offer = RequestOfferSnapshot(
        snapshot=Snapshot(
            height=record.height,
            format=record.format,
            chunks=record.chunks,
            hash=bytes.fromhex(record.snapshot_hash_hex),
            metadata=record.metadata_bytes,
        ),
        app_hash=bytes.fromhex(record.app_hash_hex),
    )
    load = RequestLoadSnapshotChunk(height=record.height, format=record.format, chunk=0)
    apply = RequestApplySnapshotChunk(index=0, chunk=record.path.read_bytes(), sender="peer")

    assert not call(app, "list_snapshots", RequestListSnapshots()).snapshots
    assert call(app, "offer_snapshot", offer).result == ResponseOfferSnapshot.ABORT
    assert call(app, "load_snapshot_chunk", load).chunk == b""
    assert call(app, "apply_snapshot_chunk", apply).result == ResponseApplySnapshotChunk.ABORT


def test_node_refuses_an_enabled_upstream_state_sync_configuration(app):
    config = dict(app.cometbft_config)
    config["statesync"] = {"enable": True}
    with patch("xian.xian_abci.load_tendermint_config", return_value=config):
        with pytest.raises(ValueError, match="ordinary replay.*verified full-node-store"):
            Xian(constants=app.constants)


def test_ordinary_echo_has_no_private_registration_protocol(app):
    message = "xian-evidence-history/1:" + "ab" * 32
    assert call(app, "echo", RequestEcho(message=message)).message == message
    assert call(app, "echo", RequestEcho(message="ping")).message == "ping"


def test_interrupted_full_store_publication_blocks_startup_before_state_writes(app):
    recovery = app.constants.COMETBFT_HOME / ".snapshot-restore-interrupted/backup/data"
    recovery.mkdir(parents=True)
    (recovery / "old-blocks").write_text("operator recovery required")
    with patch(
        "xian.xian_abci.initialize_execution_policy",
        side_effect=AssertionError("wrote before recovery"),
    ):
        with pytest.raises(ValueError, match="incomplete snapshot restore.*recovery"):
            Xian(constants=app.constants)
