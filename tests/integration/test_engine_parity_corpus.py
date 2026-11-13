"""The engine parity corpora on both paths against the golden fixtures.

``engine_parity_corpus`` runs every corpus on the Python per-transaction
path and on the production path. Both must reproduce the frozen Python-path
outputs in ``tests/fixtures/engine_parity/`` byte for byte (receipt bytes,
which carry the writes, chi, rewards and events; the block writes; the app
hash), and the production path may hand to the Python path only the cases
``handback_inventory.json`` lists. Once a rule moves into Rust, its cases
leave the inventory; once the Python path is deleted, only the golden
comparison of the production path remains.
"""

from __future__ import annotations

import pytest
from engine_parity_corpus import (
    PER_TX_PATH,
    corpora,
    handback_inventory,
    handed_back,
    load_fixture,
    run_contract_views,
    run_corpus,
    run_simulations,
    run_system_blocks,
    zk_available,
)

pytest.importorskip("xian_vm_core")

CORPORA = {corpus.name: corpus for corpus in corpora()}


def _corpus(name: str):
    corpus = CORPORA[name]
    if corpus.requires_zk and not zk_available():
        pytest.skip("the zk corpus needs the native zk verifier (the zk extra)")
    return corpus


def _assert_blocks_equal(actual: dict, expected: dict, path: str) -> None:
    assert actual["policy"] == expected["policy"]
    assert len(actual["blocks"]) == len(expected["blocks"]), path
    for block, golden in zip(actual["blocks"], expected["blocks"], strict=True):
        for receipt, golden_receipt in zip(
            block.get("txs", []), golden.get("txs", []), strict=True
        ):
            assert receipt == golden_receipt, f"{path}: {golden_receipt['case']}"
        assert block == golden, f"{path}: block {golden['height']}"


@pytest.mark.parametrize("name", sorted(CORPORA))
def test_block_corpus_matches_the_golden_fixture_on_both_paths(tmp_path, name):
    corpus = _corpus(name)
    golden = load_fixture(f"blocks_{name}")
    python, python_routes = run_corpus(tmp_path / "python", corpus, python=True)
    production, routes = run_corpus(tmp_path / "production", corpus, python=False)

    assert set(python_routes.values()) == {PER_TX_PATH}
    _assert_blocks_equal(python, golden, "python path")
    _assert_blocks_equal(production, golden, "production path")
    assert handed_back(routes) == load_fixture("handback_inventory")[name]


def test_the_inventory_lists_every_hand_back_and_outside_entry_point(tmp_path):
    # The zk hand-backs are decided by the module, with or without the verifier.
    assert handback_inventory(tmp_path) == load_fixture("handback_inventory")


@pytest.mark.parametrize("python", [True, False], ids=["python", "production"])
def test_simulations_match_the_golden_fixture(tmp_path, python):
    assert run_simulations(tmp_path, python=python) == load_fixture("simulations")


@pytest.mark.parametrize("python", [True, False], ids=["python", "production"])
def test_contract_views_match_the_golden_fixture(tmp_path, python):
    assert run_contract_views(tmp_path, python=python) == load_fixture("contract_views")


@pytest.mark.usefixtures("node_home")
@pytest.mark.parametrize("python", [True, False], ids=["python", "production"])
def test_system_transactions_match_the_golden_fixture(python):
    assert run_system_blocks(python=python) == load_fixture("system_blocks")
