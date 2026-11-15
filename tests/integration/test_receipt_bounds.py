"""A transaction cannot make a block's FinalizeBlock response outgrow its chi.

Every node builds the response, sends it to CometBFT and stores it with the
block results (one 105 MB receipt stalled a localnet block for about 14 s).
Values a contract shares cost it one copy, but the
receipt and the stored state hold every reference in full, so a value that
crosses the VM boundary is bounded like the VM heap, and a return value is
priced and capped like a state write."""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
from loguru import logger

from abci_node import FOUNDER, AbciNode, local_genesis, node_constants, signed_tx

pytest.importorskip("xian_vm_core")

SHARING = """values = Hash()

def megabyte():
    text = "x" * 100000
    grown = text
    for step in range(9):
        grown = grown + text
    return grown

@export
def returns_shared(copies: int):
    return [megabyte()] * copies

@export
def stores_shared(copies: int):
    values["shared"] = [megabyte()] * copies

@export
def returns_text(size: int):
    return "r" * size
"""
ONE_MB_COPIES = 200


@pytest.fixture(autouse=True)
def _quiet():
    logging.disable(logging.CRITICAL)
    logger.disable("xian")
    yield
    logger.enable("xian")
    logging.disable(logging.NOTSET)


async def _receipts(root, calls: list[tuple[str, dict]]) -> list[tuple[int, dict, int]]:
    node = await AbciNode(node_constants(root, local_genesis())).start()
    try:
        await node.init_chain()
        deploy = signed_tx(
            FOUNDER,
            "submission",
            "submit_contract",
            {"name": "con_sharing", "code": SHARING},
            nonce=0,
            chi=150_000,
        )
        await node.finalize_block(1, [deploy])
        await node.commit()
        txs = [
            signed_tx(FOUNDER, "con_sharing", function, kwargs, nonce=nonce, chi=200_000)
            for nonce, (function, kwargs) in enumerate(calls, start=1)
        ]
        response = await node.finalize_block(2, txs)
        await node.commit()
    finally:
        await node.stop()
    return [
        (result.code, json.loads(result.data), len(result.data)) for result in response.tx_results
    ]


def test_shared_values_cannot_cross_the_vm_boundary_as_copies(tmp_path):
    receipts = asyncio.run(
        _receipts(
            tmp_path,
            [
                ("returns_shared", {"copies": ONE_MB_COPIES}),
                ("stores_shared", {"copies": ONE_MB_COPIES}),
            ],
        )
    )
    for code, receipt, size in receipts:
        assert code == 1
        assert receipt["result"] == "'VM value traversal limit exceeded'"
        assert size < 4096


def test_a_return_value_is_priced_and_capped_like_a_write(tmp_path):
    (small, large) = asyncio.run(
        _receipts(
            tmp_path,
            [("returns_text", {"size": 100_000}), ("returns_text", {"size": 128 * 1024})],
        )
    )
    code, receipt, _ = small
    assert code == 0 and len(receipt["result"]) == 100_000
    # 25 raw chi per encoded byte, as for a write: 2,500 chi for 100 kB.
    assert receipt["chi_used"] >= 2_500
    code, receipt, size = large
    assert code == 1
    assert receipt["result"] == (
        "'You have exceeded the maximum return value size per transaction!'"
    )
    assert size < 4096
