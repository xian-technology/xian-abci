"""Only the paired Comet client may hand snapshots to the ABCI facade."""

import asyncio
import base64
import json
import shutil
from io import BytesIO
from unittest.mock import patch

import pytest
from test_evidence_retention import _evidence_query, _paid_policy_block
from test_evidence_retention import app as app

from abci.server import ProtocolHandler
from abci.utils import read_messages
from cometbft.abci.v1beta1.types_pb2 import (
    RequestApplySnapshotChunk,
    RequestEcho,
    RequestOfferSnapshot,
    ResponseOfferSnapshot,
)
from cometbft.abci.v1beta3.types_pb2 import Request, Response
from xian.xian_abci import Xian

PREFIX = "xian-evidence-history/1:"
ACK = PREFIX + "registered:"
NONCE = "ab" * 32
OTHER = "cd" * 32


def call(node, kind, request):
    payload = asyncio.run(ProtocolHandler(node).process(kind, Request(**{kind: request})))
    return getattr(next(read_messages(BytesIO(payload), Response)), kind)


def wrapped(snapshot, nonce=NONCE, **overrides):
    clone = type(snapshot)()
    clone.CopyFrom(snapshot)
    envelope = {
        "schema_version": 1,
        "xian_evidence_history_nonce": nonce,
        "metadata": base64.b64encode(snapshot.metadata).decode(),
    }
    envelope.update(overrides)
    clone.metadata = json.dumps(envelope).encode()
    return clone


@pytest.fixture
def pair(app, tmp_path):
    _paid_policy_block(app, [])
    asyncio.run(app.commit())
    app.state_snapshot_manager.export_snapshot()
    record = app.state_snapshot_manager.list_snapshot_records()[0]
    home = tmp_path / "target"
    shutil.copytree(app.constants.COMETBFT_HOME / "config", home / "config")

    class Target(app.constants):
        COMETBFT_HOME = home
        COMETBFT_CONFIG = home / "config/config.toml"
        XIAN_CONFIG = home / "config/xian.toml"
        COMETBFT_GENESIS = home / "config/genesis.json"
        STORAGE_HOME = home / "xian"

    target = Xian(constants=Target)
    yield app, target, record
    asyncio.run(target.close())


def offer(node, snapshot, app_hash):
    return call(node, "offer_snapshot", RequestOfferSnapshot(snapshot=snapshot, app_hash=app_hash))


def register(node, nonce=NONCE):
    response = call(node, "echo", RequestEcho(message=PREFIX + nonce))
    assert response.message == ACK + nonce


def test_private_echo_is_awaited_and_ordinary_echo_is_standard(app):
    response = call(app, "echo", RequestEcho(message="ping"))
    assert response.message == "ping"
    register(app)


def test_new_app_rejects_stock_comet_offer_and_unregistered_peer_wrapper(pair):
    _, target, record = pair
    snapshot = record.to_proto()
    app_hash = bytes.fromhex(record.app_hash_hex)
    assert offer(target, snapshot, app_hash).result == ResponseOfferSnapshot.REJECT
    assert offer(target, wrapped(snapshot), app_hash).result == ResponseOfferSnapshot.REJECT
    assert target.state_snapshot_manager._incoming_session is None


def test_matching_pair_accepts_original_metadata_and_restores_real_snapshot(pair):
    source, target, record = pair
    original = record.to_proto()
    original_bytes = original.SerializeToString()
    register(target)
    authenticated = wrapped(original)
    result = offer(target, authenticated, bytes.fromhex(record.app_hash_hex))
    assert result.result == ResponseOfferSnapshot.ACCEPT
    assert original.SerializeToString() == original_bytes
    for index in range(record.chunks):
        chunk = source.state_snapshot_manager.load_snapshot_chunk_response(
            height=record.height, format_version=record.format, chunk_index=index
        )
        result = call(
            target,
            "apply_snapshot_chunk",
            RequestApplySnapshotChunk(index=index, chunk=chunk.chunk, sender="source"),
        )
        assert result.result == result.ACCEPT
    response = _evidence_query(target, height=record.height)
    assert response.code == 0, response.log
    assert json.loads(response.value)["app_hash"] == record.app_hash_hex
    assert target.state_root_cache.root_hash.hex() == record.app_hash_hex
    # Capability wrappers never replace served/listed peer metadata.
    served = source.state_snapshot_manager.list_snapshots_response().snapshots[0]
    assert served.metadata == original.metadata
    assert b"xian_evidence_history_nonce" not in served.metadata
    imported = target.state_snapshot_manager.list_snapshots_response().snapshots[0]
    assert imported.metadata == original.metadata
    assert b"xian_evidence_history_nonce" not in imported.metadata


def test_replay_and_replaced_nonce_cannot_reauthorize_an_offer(pair):
    _, target, record = pair
    snapshot = record.to_proto()
    root = bytes.fromhex(record.app_hash_hex)
    register(target)
    register(target, OTHER)
    assert offer(target, wrapped(snapshot), root).result == ResponseOfferSnapshot.REJECT
    assert offer(target, wrapped(snapshot, OTHER), root).result == ResponseOfferSnapshot.REJECT
    register(target, OTHER)
    assert offer(target, wrapped(snapshot, OTHER), root).result == ResponseOfferSnapshot.ACCEPT
    assert offer(target, wrapped(snapshot, OTHER), root).result == ResponseOfferSnapshot.REJECT


def test_invalid_original_metadata_consumes_pairing_then_fresh_echo_can_retry(pair):
    _, target, record = pair
    snapshot = record.to_proto()
    bad = type(snapshot)()
    bad.CopyFrom(snapshot)
    bad.metadata = b"{}"
    root = bytes.fromhex(record.app_hash_hex)
    register(target)
    assert offer(target, wrapped(bad), root).result == ResponseOfferSnapshot.REJECT
    assert offer(target, wrapped(snapshot), root).result == ResponseOfferSnapshot.REJECT
    register(target)
    assert offer(target, wrapped(snapshot), root).result == ResponseOfferSnapshot.ACCEPT


def test_snapshot_application_uses_the_query_state_transition_lock(pair):
    _, target, _ = pair
    from cometbft.abci.v1beta1.types_pb2 import ResponseApplySnapshotChunk

    async def run():
        async with target._state_transition_lock:
            task = asyncio.create_task(target.apply_snapshot_chunk(RequestApplySnapshotChunk()))
            await asyncio.sleep(0)
            assert not task.done()
            applying.assert_not_called()
        return await task

    with patch.object(
        target.state_snapshot_manager,
        "apply_snapshot_chunk_response",
        return_value=ResponseApplySnapshotChunk(result=ResponseApplySnapshotChunk.RETRY),
    ) as applying:
        response = asyncio.run(run())
    assert response.result == response.RETRY


def test_public_info_queries_and_peer_flags_cannot_grant_capability(pair):
    _, target, record = pair
    from cometbft.abci.v1beta1.types_pb2 import RequestQuery
    from cometbft.abci.v1beta2.types_pb2 import RequestInfo

    call(target, "info", RequestInfo(version=PREFIX + NONCE))
    query = call(target, "query", RequestQuery(path="/echo/" + PREFIX + NONCE))
    assert query.code == 1
    root = bytes.fromhex(record.app_hash_hex)
    assert offer(target, wrapped(record.to_proto()), root).result == ResponseOfferSnapshot.REJECT
    register(target)
    forged = wrapped(record.to_proto(), verified=True)
    assert offer(target, forged, root).result == ResponseOfferSnapshot.REJECT


def test_restart_discards_registration_and_no_nonce_is_persisted_or_logged(pair):
    _, target, record = pair
    from loguru import logger

    from xian.state_root import StateRootCache

    before = StateRootCache.from_driver(target.client.raw_driver).root_hash
    logs = []
    sink = logger.add(lambda message: logs.append(str(message)))
    try:
        register(target)
        assert StateRootCache.from_driver(target.client.raw_driver).root_hash == before
        asyncio.run(target.close())
        restarted = Xian(constants=target.constants)
        try:
            root = bytes.fromhex(record.app_hash_hex)
            assert (
                offer(restarted, wrapped(record.to_proto()), root).result
                == ResponseOfferSnapshot.REJECT
            )
            register(restarted, OTHER)
            assert (
                offer(restarted, wrapped(record.to_proto(), OTHER), root).result
                == ResponseOfferSnapshot.ACCEPT
            )
        finally:
            asyncio.run(restarted.close())
    finally:
        logger.remove(sink)
    assert NONCE not in "".join(logs)
    assert OTHER not in "".join(logs)


def test_snapshot_replace_and_policy_query_share_one_committed_point(pair):
    source, target, _ = pair
    from test_evidence_retention import _init_request

    from cometbft.abci.v1beta1.types_pb2 import RequestQuery
    from xian.methods.query import CORE_QUERY_HANDLERS

    asyncio.run(target.init_chain(_init_request(target)))
    old = _paid_policy_block(target, [])
    asyncio.run(target.commit())
    _paid_policy_block(source, [], height=2, seconds=10 * 86400 + 9)
    asyncio.run(source.commit())
    source.state_snapshot_manager.export_snapshot()
    record = next(r for r in source.state_snapshot_manager.list_snapshot_records() if r.height == 2)
    register(target)
    assert (
        offer(target, wrapped(record.to_proto()), bytes.fromhex(record.app_hash_hex)).result
        == ResponseOfferSnapshot.ACCEPT
    )
    chunks = [
        source.state_snapshot_manager.load_snapshot_chunk_response(
            height=record.height, format_version=record.format, chunk_index=index
        ).chunk
        for index in range(record.chunks)
    ]
    for index, chunk in enumerate(chunks[:-1]):
        call(
            target,
            "apply_snapshot_chunk",
            RequestApplySnapshotChunk(index=index, chunk=chunk, sender="source"),
        )
    original = CORE_QUERY_HANDLERS["evidence_policy"]

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        async def paused(ctx):
            entered.set()
            await release.wait()
            return await original(ctx)

        with patch.dict(CORE_QUERY_HANDLERS, {"evidence_policy": paused}):
            querying = asyncio.create_task(target.query(RequestQuery(path="/evidence_policy")))
            await entered.wait()
            replacing = asyncio.create_task(
                target.apply_snapshot_chunk(
                    RequestApplySnapshotChunk(
                        index=len(chunks) - 1, chunk=chunks[-1], sender="source"
                    )
                )
            )
            await asyncio.sleep(0)
            release.set()
            before, imported = await querying, await replacing
        after = await target.query(RequestQuery(path="/evidence_policy"))
        return before, imported, after

    before, imported, after = asyncio.run(run())
    assert imported.result == imported.ACCEPT
    assert before.height == 1
    assert json.loads(before.value)["height"] == "1"
    assert json.loads(before.value)["app_hash"] == old.app_hash.hex()
    assert after.height == 2
    assert json.loads(after.value)["height"] == "2"
    assert json.loads(after.value)["app_hash"] == record.app_hash_hex


def test_paired_negative_offer_reaches_original_format_validator(pair):
    _, target, record = pair
    snapshot = record.to_proto()
    snapshot.format += 1
    register(target)
    result = offer(target, wrapped(snapshot), bytes.fromhex(record.app_hash_hex))
    assert result.result == ResponseOfferSnapshot.REJECT_FORMAT


def test_offer_uses_durable_current_height_even_when_json_mirror_is_stale(pair):
    _, target, record = pair
    from test_evidence_retention import _init_request

    from xian.utils.block import set_latest_block

    asyncio.run(target.init_chain(_init_request(target)))
    _paid_policy_block(target, [])
    asyncio.run(target.commit())
    set_latest_block(height=0, storage_home=target.client.raw_driver.storage_home)
    register(target)
    # A stale local mirror must not authorize replacing the already-committed height.
    assert (
        offer(target, wrapped(record.to_proto()), bytes.fromhex(record.app_hash_hex)).result
        == ResponseOfferSnapshot.REJECT
    )
