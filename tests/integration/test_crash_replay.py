"""A node killed anywhere between FinalizeBlock and the end of Commit and
restarted replays to the same blocks as a node that never stopped: the same
app hashes, receipts (LastResultsHash) and validator updates.

CometBFT's handshake replays the block the application did not commit
(Info reports the last committed height) and calls InitChain again while no
block is committed."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest
from abci_node import (
    FOUNDER,
    AbciNode,
    block_digest,
    local_genesis,
    node_constants,
    signed_tx,
)
from xian_accounts import Ed25519Account

from tendermint.abci.types_pb2 import RequestProcessProposal, ResponseProcessProposal

pytest.importorskip("xian_vm_core")
pytestmark = pytest.mark.usefixtures("quiet_node_logs")

BOB = Ed25519Account("22" * 32)
COUNTER = """counter = Variable()

@export
def bump(by: int):
    assert by > 0, "by must be positive"
    counter.set((counter.get() or 0) + by)
    return counter.get()
"""


def _blocks() -> list[list[bytes]]:
    return [
        [
            signed_tx(
                FOUNDER, "currency", "transfer", {"amount": 100_000, "to": BOB.public_key}, nonce=0
            )
        ],
        [
            signed_tx(
                FOUNDER,
                "submission",
                "submit_contract",
                {"name": "con_counter", "code": COUNTER},
                nonce=1,
                chi=150_000,
            ),
            # Fails: more than BOB holds.
            signed_tx(
                BOB, "currency", "transfer", {"amount": 10**9, "to": FOUNDER.public_key}, nonce=0
            ),
        ],
        [
            signed_tx(FOUNDER, "con_counter", "bump", {"by": 2}, nonce=2),
            # Fails inside the contract; its fee is still paid.
            signed_tx(BOB, "con_counter", "bump", {"by": 0}, nonce=1),
            signed_tx(BOB, "currency", "approve", {"amount": 3, "to": FOUNDER.public_key}, nonce=2),
        ],
        [signed_tx(FOUNDER, "currency", "transfer", {"amount": 7, "to": BOB.public_key}, nonce=3)],
    ]


async def _run_chain(root, *, crash=None) -> list:
    """Run every block; ``crash(height, node)`` returns whether the node
    was stopped after FinalizeBlock of ``height`` (and runs the stop)."""
    node = await AbciNode(node_constants(root, local_genesis())).start()
    digests = [await node.init_chain()]
    try:
        for height, txs in enumerate(_blocks(), start=1):
            response = await node.finalize_block(height, txs)
            if crash is not None and await crash(height, node):
                await node.start()
                assert (await node.info()).last_block_height == height - 1
                response = await node.finalize_block(height, txs)
            await node.commit()
            digests.append(block_digest(response))
    finally:
        await node.stop()
    return digests


@pytest.fixture
def reference(tmp_path):
    digests = asyncio.run(_run_chain(tmp_path / "reference"))
    # The scenario exercises successes and failures alike.
    codes = [code for _, results, _ in digests[1:] for code, _, _ in results]
    assert 0 in codes and 1 in codes
    return digests


@pytest.mark.parametrize("crash_height", [1, 2, 3, 4])
def test_a_node_killed_before_commit_replays_the_block(tmp_path, reference, crash_height):
    async def crash(height, node):
        if height != crash_height:
            return False
        await node.stop()
        return True

    assert asyncio.run(_run_chain(tmp_path / "crashed", crash=crash)) == reference


def test_a_node_killed_after_init_chain_runs_init_chain_again(tmp_path, reference):
    assert asyncio.run(_init_chain_twice(tmp_path)) == reference


def test_warm_admission_cache_preserves_exact_ordered_finalize_responses(tmp_path):
    blocks = _blocks()

    async def run_blocks(root, *, use_cache):
        node = await AbciNode(node_constants(root, local_genesis())).start()
        try:
            cache = node.app.transaction_admission_cache
            if not use_cache:
                node.app.transaction_admission_cache = None
            elif cache is None:
                pytest.skip("the installed native VM wheel predates admission caching")

            await node.init_chain()
            responses = []
            for height, txs in enumerate(blocks, start=1):
                proposal = await node.app.process_proposal(RequestProcessProposal(txs=txs))
                assert proposal.status == ResponseProcessProposal.ACCEPT
                response = await node.finalize_block(height, txs)
                responses.append(response.SerializeToString(deterministic=True))
                await node.commit()
            if use_cache:
                assert cache.stats()["hits"] == sum(map(len, blocks))
            return responses
        finally:
            await node.stop()

    assert asyncio.run(run_blocks(tmp_path / "warm", use_cache=True)) == asyncio.run(
        run_blocks(tmp_path / "uncached", use_cache=False)
    )


@pytest.mark.parametrize("indexing, tracing", [(False, False), (True, False), (False, True)])
def test_native_receipts_match_python_with_warm_admission(tmp_path, monkeypatch, indexing, tracing):
    monkeypatch.setenv("XIAN_PERF_ENABLED", "1")

    async def run(root, native):
        node = await AbciNode(node_constants(root, local_genesis())).start()
        try:
            processor = node.app.tx_processor
            assert processor.native_receipts
            processor.native_receipts = native
            processor.trace_logging = tracing
            await node.init_chain()
            if indexing:
                node.app.bds_enabled = True
                node.app.bds = AsyncMock()
            responses = []
            for height, txs in enumerate(_blocks(), start=1):
                proposal = await node.app.process_proposal(RequestProcessProposal(txs=txs))
                assert proposal.status == ResponseProcessProposal.ACCEPT
                response = await node.finalize_block(height, txs)
                responses.append(response.SerializeToString(deterministic=True))
                await node.commit()

            metrics = node.app.profiler.global_metrics
            assert ("tx_native_receipt_encoding" in metrics) == native
            assert metrics["tx_native_execute"].count >= len(responses)
            assert node.app.transaction_admission_cache.stats()["hits"] == sum(map(len, _blocks()))
            if indexing:
                spool = [
                    json.dumps(call.args[0].to_spool_dict(), separators=(",", ":")).encode()
                    for call in node.app.bds.enqueue_block.await_args_list
                ]
                assert len(spool) == len(responses)
            else:
                spool = []
            return responses, spool
        finally:
            await node.stop()

    assert asyncio.run(run(tmp_path / "native", True)) == asyncio.run(
        run(tmp_path / "python", False)
    )


async def _init_chain_twice(tmp_path):
    constants = node_constants(tmp_path / "crashed", local_genesis())
    node = await AbciNode(constants).start()
    first = await node.init_chain()
    await node.stop()
    await node.start()
    assert (await node.info()).last_block_height == 0
    assert await node.init_chain() == first
    digests = [first]
    for height, txs in enumerate(_blocks(), start=1):
        digests.append(block_digest(await node.finalize_block(height, txs)))
        await node.commit()
    await node.stop()
    return digests


@pytest.mark.parametrize(
    "fault_point",
    ["xian.methods.commit.try_write_latest_block", "xian.xian_abci.Xian.refresh_tx_fee_policy"],
)
def test_a_node_killed_after_the_durable_commit_resumes_at_the_next_block(
    tmp_path, reference, fault_point
):
    """Killed after the LMDB commit (before the JSON mirror or the policy
    refresh): Info reports the committed block and CometBFT replays nothing."""
    assert asyncio.run(_killed_after_commit(tmp_path, fault_point)) == reference


async def _killed_after_commit(tmp_path, fault_point):
    node = await AbciNode(node_constants(tmp_path / "crashed", local_genesis())).start()
    digests = [await node.init_chain()]
    for height, txs in enumerate(_blocks(), start=1):
        response = await node.finalize_block(height, txs)
        digests.append(block_digest(response))
        if height == 2:
            with patch(fault_point, side_effect=SystemExit("killed")), pytest.raises(SystemExit):
                await node.commit()
            await node.stop()
            await node.start()
            info = await node.info()
            assert (info.last_block_height, info.last_block_app_hash) == (2, response.app_hash)
            continue
        await node.commit()
    await node.stop()
    return digests
