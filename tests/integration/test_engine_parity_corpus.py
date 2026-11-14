"""The engine parity corpora against the golden fixtures.

``engine_parity_corpus`` runs every corpus on the production path (the Rust
block executor, ``execute_tx`` for simulations and contract views,
FinalizeBlock for system transactions). It must reproduce the frozen outputs
in ``tests/fixtures/engine_parity/`` byte for byte: receipt bytes (which
carry the writes, chi, rewards and events), the block writes and the app
hash. Until increment 9 of the single-engine plan the Python per-transaction
path was compared with them too.
"""

from __future__ import annotations

import pytest
from engine_parity_corpus import (
    corpora,
    load_fixture,
    run_contract_views,
    run_corpus,
    run_simulations,
    run_system_blocks,
)

pytest.importorskip("xian_vm_core")

CORPORA = {corpus.name: corpus for corpus in corpora()}


def _assert_blocks_equal(actual: dict, expected: dict) -> None:
    assert actual["policy"] == expected["policy"]
    assert len(actual["blocks"]) == len(expected["blocks"])
    for block, golden in zip(actual["blocks"], expected["blocks"], strict=True):
        for receipt, golden_receipt in zip(
            block.get("txs", []), golden.get("txs", []), strict=True
        ):
            assert receipt == golden_receipt, golden_receipt["case"]
        assert block == golden, f"block {golden['height']}"


@pytest.mark.parametrize("name", sorted(CORPORA))
def test_block_corpus_matches_the_golden_fixture(tmp_path, name):
    # Every corpus runs: the VM links the zk verifier.
    _assert_blocks_equal(run_corpus(tmp_path, CORPORA[name]), load_fixture(f"blocks_{name}"))


def test_simulations_match_the_golden_fixture(tmp_path):
    assert run_simulations(tmp_path) == load_fixture("simulations")


def test_contract_views_match_the_golden_fixture(tmp_path):
    assert run_contract_views(tmp_path) == load_fixture("contract_views")


@pytest.mark.usefixtures("node_home")
def test_system_transactions_match_the_golden_fixture():
    assert run_system_blocks() == load_fixture("system_blocks")
